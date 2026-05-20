"""
Background Scan Runner - Runs scan sessions asynchronously in a background thread daemon.
"""
import queue
import threading
import logging
import time
from urllib.parse import urlparse
from django.db import transaction, connection
from django.utils import timezone

from .models import ScanSession, Keyword, Whitelist, ScrapedPage, DetectedContent, ScanLog
from .scraper import WebScraper
from .detection import ContentDetector

logger = logging.getLogger(__name__)

# Thread-safe queue for scan session PKs
_queue = queue.Queue()
_started = False
_lock = threading.Lock()

class ThreadSafeBatchedDbWriter:
    """Thread-safe batch writer to group database writes in transactional blocks on SQLite"""
    
    def __init__(self, scan_session: ScanSession, batch_size=15, time_limit=2.0):
        self.scan_session = scan_session
        self.batch_size = batch_size
        self.time_limit = time_limit
        self.lock = threading.Lock()
        
        # Buffers
        self.pages_buffer = []      # list of dicts of page data + detections
        self.logs_buffer = []       # list of ScanLog instances
        
        self.last_flush_time = time.time()
        self.total_issues_count = 0
        self.pages_scraped_count = 0
        
        self.seen_errors = set()
        self.seen_whitelisted = set()

    def add_page(self, result, page_status, error_message, detections_data, pages_scraped,
                 content_hash='', etag='', last_modified='', has_cloaking=False):
        with self.lock:
            self.pages_scraped_count = pages_scraped
            
            self.pages_buffer.append({
                'url': result['url'],
                'domain': result.get('domain', '') or '',
                'title': (result.get('title', '') or '')[:500],
                'meta_description': result.get('meta_description', '') or '',
                'content': result.get('content', '') or '',
                'http_status': result.get('http_status'),
                'status': page_status,
                'error_message': error_message or '',
                'detections': detections_data,
                'content_hash': content_hash or result.get('content_hash', ''),
                'etag': etag or result.get('etag', ''),
                'last_modified': last_modified or result.get('last_modified', ''),
                'has_cloaking': has_cloaking or result.get('has_cloaking', False),
            })
            
            self.total_issues_count += len(detections_data)
            
            current_time = time.time()
            if len(self.pages_buffer) >= self.batch_size or (current_time - self.last_flush_time) >= self.time_limit:
                self._flush()

    def add_log(self, log_type, message, url=''):
        with self.lock:
            self.logs_buffer.append(ScanLog(
                scan_session=self.scan_session,
                log_type=log_type,
                message=message,
                url=url
            ))
            
            # Flush immediately for high-priority warning/error/success logs if needed,
            # or let them be batched. Let's let them batch but flush logs if we have > 50 logs.
            if len(self.logs_buffer) >= 50:
                self._flush()

    def force_flush(self):
        with self.lock:
            self._flush()

    def _flush(self):
        if not self.pages_buffer and not self.logs_buffer:
            return
            
        try:
            with transaction.atomic():
                # 1. Save all pages and their detections
                for p in self.pages_buffer:
                    page, _ = ScrapedPage.objects.get_or_create(
                        scan_session=self.scan_session,
                        url=p['url'],
                        defaults={
                            'domain': p['domain'],
                            'title': p['title'],
                            'meta_description': p['meta_description'],
                            'content': p['content'],
                            'http_status': p['http_status'],
                            'status': p['status'],
                            'error_message': p['error_message'],
                            'content_hash': p.get('content_hash', ''),
                            'etag': p.get('etag', ''),
                            'last_modified': p.get('last_modified', ''),
                            'has_cloaking': p.get('has_cloaking', False),
                            'scraped_at': timezone.now()
                        }
                    )
                    # Update etag/last_modified even for existing pages (keep freshest)
                    update_fields = []
                    if not page.content_hash and p.get('content_hash'):
                        page.content_hash = p['content_hash']
                        update_fields.append('content_hash')
                    if not page.etag and p.get('etag'):
                        page.etag = p['etag']
                        update_fields.append('etag')
                    if not page.last_modified and p.get('last_modified'):
                        page.last_modified = p['last_modified']
                        update_fields.append('last_modified')
                    if not page.has_cloaking and p.get('has_cloaking'):
                        page.has_cloaking = True
                        update_fields.append('has_cloaking')
                    if update_fields:
                        page.save(update_fields=update_fields)
                    
                    # Grouped detections
                    for category, det in p['detections'].items():
                        DetectedContent.objects.create(
                            page=page,
                            keyword=det['keyword_obj'],
                            matched_text=det['matched_text'][:200],
                            context=det['context'][:1000],
                            category=category,
                            severity=det['severity'],
                            location=det['location'],
                            confidence_score=det['confidence_score'],
                            safe_context_found=det['safe_context_found'],
                            match_count=det['match_count'],
                            unique_keywords=det['unique_keywords'],
                            sample_contexts=det['sample_contexts']
                        )
                
                # Clear pages buffer
                self.pages_buffer = []
                self.last_flush_time = time.time()
                
                # 2. Bulk create scan logs
                if self.logs_buffer:
                    ScanLog.objects.bulk_create(self.logs_buffer)
                    self.logs_buffer = []
                
                # 3. Update scan session stats
                self.scan_session.pages_scanned = self.pages_scraped_count
                self.scan_session.issues_found = self.total_issues_count
                self.scan_session.save(update_fields=['pages_scanned', 'issues_found'])
                
        except Exception as e:
            logger.exception(f"Error flushing batch to SQLite: {e}")


def _run_scan(session_pk: int):
    """Core scanning workflow executed on the background thread"""
    # Ensure database connection is clean/ready for thread
    connection.close_if_unusable_or_obsolete()
    
    try:
        scan_session = ScanSession.objects.get(pk=session_pk)
    except ScanSession.DoesNotExist:
        logger.error(f"ScanSession with pk={session_pk} does not exist.")
        return

    # Check for cancellation before starting
    if scan_session.should_stop():
        scan_session.cancel()
        return

    # Start scan session in database
    scan_session.start()
    
    # Initialize Batched Db Writer
    db_writer = ThreadSafeBatchedDbWriter(scan_session, batch_size=15, time_limit=2.0)
    db_writer.add_log('info', f'Memulai pemindaian {scan_session.target_url}')
    
    try:
        parsed = urlparse(scan_session.target_url)
        domain = parsed.netloc
        
        # Load content detector keywords and whitelist
        detector = ContentDetector()
        active_keywords = Keyword.objects.filter(is_active=True)
        detector.load_keywords_from_db(active_keywords)
        
        keyword_map = {kw.keyword.lower(): kw for kw in active_keywords}
        keyword_strength_map = {kw.keyword.lower(): kw.strength for kw in active_keywords}

        active_whitelist = Whitelist.objects.filter(is_active=True)
        detector.load_whitelist_from_db(active_whitelist)
        
        # Load stored ETag/Last-Modified from previous scans for incremental updates
        stored_metadata = {}
        previous_pages = ScrapedPage.objects.filter(
            url__isnull=False
        ).exclude(etag='', last_modified='').only('url', 'etag', 'last_modified')
        # Build dict only for pages where both etag and last_modified exist
        for pp in previous_pages:
            if pp.etag or pp.last_modified:
                stored_metadata[pp.url] = (pp.etag, pp.last_modified)
        
        # Initialize concurrent crawler
        scraper = WebScraper(
            base_domain=domain,
            delay=0.2,
            timeout=30,
            max_pages=scan_session.max_pages,
            scan_subdomains=scan_session.scan_subdomains,
            max_workers=12,
        )
        
        def process_page_realtime(result, pages_scraped, max_pages):
            # Check for scan cancellation
            if scan_session.should_stop():
                db_writer.add_log('warning', 'Scan dibatalkan oleh pengguna')
                scraper.cancel()
                return

            url = result.get('url', '')
            
            # Handle incremental/skip results
            if result.get('unchanged'):
                page_status = 'unchanged'
                if pages_scraped % 10 == 0:
                    db_writer.add_log('success', f'[{pages_scraped}/{max_pages}] Tidak berubah (304): {url[:60]}', url=url)
                db_writer.add_page(
                    result=result, page_status=page_status, error_message='',
                    detections_data={}, pages_scraped=pages_scraped,
                    content_hash='', etag=result.get('etag', ''), last_modified=result.get('last_modified', ''),
                )
                return
            
            if result.get('duplicate_hash'):
                page_status = 'duplicate'
                db_writer.add_page(
                    result=result, page_status=page_status, error_message='Duplicate content',
                    detections_data={}, pages_scraped=pages_scraped,
                    content_hash=result.get('content_hash', ''),
                )
                return
            
            # Log success/failure with throttling
            page_status = 'failed'
            error_message = ''
            if result.get('success'):
                page_status = 'scraped'
                if pages_scraped % 10 == 0 or pages_scraped == max_pages:
                    db_writer.add_log('success', f'[{pages_scraped}/{max_pages}] Berhasil: {url[:80]}', url=url)
            else:
                error_message = result.get('error', 'Unknown error')
                if error_message not in db_writer.seen_errors:
                    db_writer.seen_errors.add(error_message)
                    db_writer.add_log('error', f'[{pages_scraped}/{max_pages}] Gagal: {url[:60]} - {error_message}', url=url)
            
            # Stage A (two-stage): quick Aho-Corasick check on raw body
            # If no hits at all, skip full parsing + detection
            category_groups = {}
            if result.get('success') and not result.get('duplicate_hash'):
                raw_text = result.get('raw_text', '')
                if raw_text and not detector.has_any_hits(raw_text):
                    if pages_scraped % 50 == 0:
                        db_writer.add_log('info', f'⚡ Stage A clean: {url[:60]} (skipped deep scan)', url=url)
                    db_writer.add_page(
                        result=result, page_status=page_status, error_message=error_message,
                        detections_data={}, pages_scraped=pages_scraped,
                        content_hash=result.get('content_hash', ''),
                        etag=result.get('etag', ''), last_modified=result.get('last_modified', ''),
                        has_cloaking=result.get('has_cloaking', False),
                    )
                    return
                
                # Stage B: full detection
                has_cloaking = result.get('has_cloaking', False)
                cloaked_snippets = result.get('cloaked_snippets', [])
                
                detections, confidence_score, safe_context = detector.detect_in_sections(
                    title=result.get('title', ''),
                    meta_description=result.get('meta_description', ''),
                    content=result.get('content', ''),
                    url=url,
                    outbound_links=result.get('outbound_links'),
                    keyword_strengths=keyword_strength_map,
                    has_cloaking=has_cloaking,
                    cloaked_snippets=cloaked_snippets,
                )
                
                safe_context_str = ', '.join(safe_context) if safe_context else ''
                
                # Group detections on the page by category
                for d in detections:
                    # Skip if whitelisted
                    if detector.is_whitelisted(url, d['keyword']):
                        whitelist_key = (url, d['keyword'])
                        if whitelist_key not in db_writer.seen_whitelisted:
                            db_writer.seen_whitelisted.add(whitelist_key)
                            db_writer.add_log('info', f'⏭️ Dilewati (whitelist): "{d["keyword"]}" di {url[:50]}', url=url)
                        continue
                    
                    category = d['category']
                    keyword_obj = keyword_map.get(d['keyword'].lower())
                    
                    if category not in category_groups:
                        category_groups[category] = {
                            'keyword_obj': keyword_obj,
                            'matched_text': d['matched_text'],
                            'context': d['context'],
                            'severity': d.get('severity', 'medium'),
                            'location': d.get('location', 'content'),
                            'confidence_score': confidence_score,
                            'safe_context_found': safe_context_str,
                            'match_count': 0,
                            'unique_keywords': set(),
                            'sample_contexts': set()
                        }
                    
                    group = category_groups[category]
                    group['match_count'] += 1
                    group['unique_keywords'].add(d['keyword'])
                    group['sample_contexts'].add(d['context'][:200])
                    
                    # Update severity/main details based on severity precedence: high > medium > low
                    sev_map = {'high': 3, 'medium': 2, 'low': 1}
                    if sev_map.get(d.get('severity', 'medium'), 2) > sev_map.get(group['severity'], 2):
                        group['severity'] = d.get('severity', 'medium')
                        group['matched_text'] = d['matched_text']
                        group['context'] = d['context']
                        group['location'] = d.get('location', 'content')
                
                # Convert sets to list for JSON serialization and log detections
                for category, group in category_groups.items():
                    group['unique_keywords'] = list(group['unique_keywords'])
                    group['sample_contexts'] = list(group['sample_contexts'])
                    
                    # Add ScanLog detection warning only if confidence_score >= 0.7
                    if confidence_score >= 0.7:
                        category_display = {
                            'judol': 'Judi Online',
                            'obat_penguat': 'Obat Penguat',
                            'obat_aborsi': 'Obat Aborsi',
                            'konten_dewasa': 'Konten Dewasa',
                            'penipuan': 'Penipuan'
                        }.get(category, category)
                        
                        confidence_label = ''
                        if confidence_score < 0.5:
                            confidence_label = ' [⚠️ Mungkin False Positive]'
                        elif confidence_score < 0.8:
                            confidence_label = ' [Perlu Review]'
                        
                        db_writer.add_log(
                            'warning', 
                            f'🚨 TERDETEKSI: "{group["matched_text"][:50]}" [{category_display}]{confidence_label} (Total: {group["match_count"]}x)', 
                            url=url
                        )
            
            # Buffer result to database batch
            db_writer.add_page(
                result=result,
                page_status=page_status,
                error_message=error_message,
                detections_data=category_groups,
                pages_scraped=pages_scraped,
                content_hash=result.get('content_hash', ''),
                etag=result.get('etag', ''),
                last_modified=result.get('last_modified', ''),
                has_cloaking=result.get('has_cloaking', False),
            )
            
        # Run Web Crawler
        results = scraper.crawl(scan_session.target_url, callback=process_page_realtime, stored_metadata=stored_metadata)
        
        # Flush any remaining items in the buffer
        db_writer.force_flush()
        
        # Finalize Scan Session
        scan_session.refresh_from_db(fields=['is_cancelled'])
        if scan_session.is_cancelled:
            db_writer.add_log('warning', 'Scan selesai dengan status dibatalkan.')
        else:
            scan_session.pages_scanned = len(results)
            scan_session.complete()
            db_writer.add_log('success', f'✅ Scan selesai! {len(results)} halaman, {db_writer.total_issues_count} konten negatif')
            
    except Exception as e:
        logger.exception(f"Background thread scan crash for session {session_pk}")
        db_writer.force_flush()
        scan_session.fail(str(e))
        db_writer.add_log('error', f'Error fatal scan: {str(e)[:200]}')
    finally:
        connection.close()


def _worker_loop():
    """Infinite loop fetching and running scan sessions from the queue"""
    while True:
        try:
            session_pk = _queue.get()
            _run_scan(session_pk)
        except Exception as e:
            logger.exception("Exception in background worker loop")
        finally:
            _queue.task_done()


def enqueue(session_pk: int):
    """Enqueue a scan session to the background thread runner"""
    global _started
    with _lock:
        if not _started:
            t = threading.Thread(target=_worker_loop, daemon=True, name="ScanWorkerThread")
            t.start()
            _started = True
            logger.info("Background ScanWorkerThread daemon started.")
    _queue.put(session_pk)
    logger.info(f"ScanSession {session_pk} enqueued to background runner.")
