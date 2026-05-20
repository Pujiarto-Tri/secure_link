"""
Views for the detector application
"""
import csv
import json
import logging
import time
from io import StringIO
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.generic import ListView, DetailView, TemplateView
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.db.models import Count, Q

from .models import Keyword, ScanSession, ScrapedPage, DetectedContent, ScanLog, Whitelist
from .scraper import WebScraper
from .detection import ContentDetector

logger = logging.getLogger(__name__)


class DashboardView(TemplateView):
    """Dashboard utama aplikasi"""
    template_name = 'detector/dashboard.html'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Statistik deteksi
        context['total_scans'] = ScanSession.objects.count()
        context['active_scans'] = ScanSession.objects.filter(status='running').count()
        context['total_pages'] = ScrapedPage.objects.filter(status='scraped').count()
        context['total_detections'] = DetectedContent.objects.count()
        context['unresolved_detections'] = DetectedContent.objects.filter(is_resolved=False).count()
        context['reported_detections'] = DetectedContent.objects.filter(is_reported=True).count()
        
        # Deteksi per kategori
        context['detections_by_category'] = (
            DetectedContent.objects
            .values('category')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        
        # Scan sessions terbaru
        context['recent_scans'] = ScanSession.objects.order_by('-created_at')[:5]
        
        # Deteksi terbaru yang belum ditangani
        context['recent_unresolved'] = (
            DetectedContent.objects
            .filter(is_resolved=False)
            .select_related('page')
            .order_by('-detected_at')[:10]
        )
        
        # Statistik 7 hari terakhir
        seven_days_ago = timezone.now() - timedelta(days=7)
        context['weekly_detections'] = (
            DetectedContent.objects
            .filter(detected_at__gte=seven_days_ago)
            .count()
        )
        
        return context


class StartScanView(View):
    """View untuk memulai scan baru"""
    template_name = 'detector/start_scan.html'
    
    def get(self, request):
        return render(request, self.template_name)
    
    def post(self, request):
        target_url = request.POST.get('target_url', '').strip()
        max_pages = int(request.POST.get('max_pages', 50))
        scan_subdomains = request.POST.get('scan_subdomains') == 'on'
        
        if not target_url:
            messages.error(request, 'URL target harus diisi')
            return redirect('detector:start_scan')
        
        # Validasi URL
        if not target_url.startswith(('http://', 'https://')):
            target_url = 'https://' + target_url
        
        # Buat scan session baru
        scan_session = ScanSession.objects.create(
            target_url=target_url,
            max_pages=max_pages,
            scan_subdomains=scan_subdomains,
            status='pending'
        )
        
        # Redirect ke halaman processing
        return redirect('detector:run_scan', pk=scan_session.pk)

class RunScanView(View):
    """View untuk menjalankan scan (akan dipanggil via AJAX)"""
    
    def get(self, request, pk):
        scan_session = get_object_or_404(ScanSession, pk=pk)
        return render(request, 'detector/run_scan.html', {'scan': scan_session})
    
    def post(self, request, pk):
        scan_session = get_object_or_404(ScanSession, pk=pk)
        
        if scan_session.status not in ['pending', 'failed']:
            return JsonResponse({
                'success': False, 
                'error': 'Scan sudah berjalan atau selesai'
            })
        
        # Mulai scan
        scan_session.start()
        
        # Log mulai scan
        ScanLog.objects.create(
            scan_session=scan_session,
            log_type='info',
            message=f'Memulai pemindaian {scan_session.target_url}'
        )
        
        try:
            # Parse domain dari URL
            from urllib.parse import urlparse
            parsed = urlparse(scan_session.target_url)
            domain = parsed.netloc
            
            # Inisialisasi detector SEBELUM crawling agar bisa deteksi real-time
            detector = ContentDetector()
            active_keywords = Keyword.objects.filter(is_active=True)
            detector.load_keywords_from_db(active_keywords)
            
            # Buat in-memory keyword map untuk pencarian cepat tanpa query SQL berulang
            keyword_map = {kw.keyword.lower(): kw for kw in active_keywords}
            
            # Load whitelist untuk filter false positives
            active_whitelist = Whitelist.objects.filter(is_active=True)
            detector.load_whitelist_from_db(active_whitelist)
            
            # State throttling dan cache
            last_save_time = [time.time()]
            seen_errors = set()
            seen_whitelisted = set()
            
            # Counter untuk total issues (menggunakan list agar bisa dimodifikasi dalam closure)
            total_issues = [0]
            
            # Inisialisasi scraper
            scraper = WebScraper(
                base_domain=domain,
                delay=0.2,
                timeout=30,
                max_pages=scan_session.max_pages,
                scan_subdomains=scan_session.scan_subdomains,
                max_workers=12
            )
            
            # Callback untuk REAL-TIME processing: crawl + detect + save
            def process_page_realtime(result, pages_scraped, max_pages):
                # Cek apakah scan dibatalkan
                if scan_session.should_stop():
                    ScanLog.objects.create(
                        scan_session=scan_session,
                        log_type='warning',
                        message='Scan dibatalkan oleh pengguna'
                    )
                    scraper.cancel()
                    return
                
                url = result.get('url', '')
                
                # Log progress - Throttle success logging to every 10 pages or last page
                if result.get('success'):
                    if pages_scraped % 10 == 0 or pages_scraped == max_pages:
                        ScanLog.objects.create(
                            scan_session=scan_session,
                            log_type='success',
                            message=f'[{pages_scraped}/{max_pages}] Berhasil: {url[:80]}',
                            url=url
                        )
                else:
                    error_msg = result.get('error', 'Unknown error')
                    if error_msg not in seen_errors:
                        seen_errors.add(error_msg)
                        ScanLog.objects.create(
                            scan_session=scan_session,
                            log_type='error',
                            message=f'[{pages_scraped}/{max_pages}] Gagal: {url[:60]} - {error_msg}',
                            url=url
                        )
                
                # SIMPAN halaman ke database SEGERA
                page, created = ScrapedPage.objects.get_or_create(
                    scan_session=scan_session,
                    url=result['url'],
                    defaults={
                        'domain': result.get('domain', '') or '',
                        'title': (result.get('title', '') or '')[:500],
                        'meta_description': result.get('meta_description', '') or '',
                        'content': result.get('content', '') or '',
                        'http_status': result.get('http_status'),
                        'status': 'scraped' if result.get('success') else 'failed',
                        'error_message': result.get('error') or '',
                        'scraped_at': timezone.now()
                    }
                )
                
                # DETEKSI konten negatif SEGERA (real-time)
                if result.get('success'):
                    url = result.get('url', '')
                    detections, confidence_score, safe_context = detector.detect_in_sections(
                        title=result.get('title', ''),
                        meta_description=result.get('meta_description', ''),
                        content=result.get('content', ''),
                        url=url
                    )
                    
                    safe_context_str = ', '.join(safe_context) if safe_context else ''
                    
                    for detection in detections:
                        # Skip jika URL+keyword di-whitelist
                        if detector.is_whitelisted(url, detection['keyword']):
                            whitelist_key = (url, detection['keyword'])
                            if whitelist_key not in seen_whitelisted:
                                seen_whitelisted.add(whitelist_key)
                                ScanLog.objects.create(
                                    scan_session=scan_session,
                                    log_type='info',
                                    message=f'⏭️ Dilewati (whitelist): "{detection["keyword"]}" di {url[:50]}',
                                    url=url
                                )
                            continue
                        
                        keyword_obj = keyword_map.get(detection['keyword'].lower())
                        
                        DetectedContent.objects.create(
                            page=page,
                            keyword=keyword_obj,
                            matched_text=detection['matched_text'][:200],
                            context=detection['context'][:1000],
                            category=detection['category'],
                            severity=detection.get('severity', 'medium'),
                            location=detection.get('location', 'content'),
                            confidence_score=confidence_score,
                            safe_context_found=safe_context_str
                        )
                        total_issues[0] += 1
                        
                        # Log DETEKSI dengan format khusus agar bisa di-parse di frontend
                        # Hanya tulis di ScanLog jika confidence_score >= 0.7
                        if confidence_score >= 0.7:
                            category_display = {
                                'judol': 'Judi Online',
                                'obat_penguat': 'Obat Penguat',
                                'obat_aborsi': 'Obat Aborsi',
                                'konten_dewasa': 'Konten Dewasa',
                                'penipuan': 'Penipuan'
                            }.get(detection['category'], detection['category'])
                            
                            # Tampilkan confidence level
                            confidence_label = ''
                            if confidence_score < 0.5:
                                confidence_label = ' [⚠️ Mungkin False Positive]'
                            elif confidence_score < 0.8:
                                confidence_label = ' [Perlu Review]'
                            
                            ScanLog.objects.create(
                                scan_session=scan_session,
                                log_type='warning',
                                message=f'🚨 TERDETEKSI: "{detection["matched_text"][:50]}" [{category_display}]{confidence_label}',
                                url=url
                            )
                
                # Update progress - Save progress at most once every 2 seconds or on the very last page
                current_time = time.time()
                if current_time - last_save_time[0] >= 2.0 or pages_scraped == max_pages:
                    scan_session.pages_scanned = pages_scraped
                    scan_session.issues_found = total_issues[0]
                    scan_session.save(update_fields=['pages_scanned', 'issues_found'])
                    last_save_time[0] = current_time
            
            # Jalankan crawling dengan real-time processing
            results = scraper.crawl(scan_session.target_url, callback=process_page_realtime)
            
            # Update final stats
            scan_session.pages_scanned = len(results)
            scan_session.issues_found = total_issues[0]
            scan_session.complete()
            
            ScanLog.objects.create(
                scan_session=scan_session,
                log_type='success',
                message=f'✅ Scan selesai! {len(results)} halaman, {total_issues[0]} konten negatif'
            )
            
            return JsonResponse({
                'success': True,
                'pages_scanned': len(results),
                'issues_found': total_issues[0],
                'redirect_url': f'/scan/{scan_session.pk}/'
            })
            
        except Exception as e:
            logger.exception("Error during scan")
            scan_session.fail(str(e))
            ScanLog.objects.create(
                scan_session=scan_session,
                log_type='error',
                message=f'Error: {str(e)}'
            )
            return JsonResponse({
                'success': False,
                'error': str(e)
            })


class CancelScanView(View):
    """View untuk membatalkan scan yang sedang berjalan"""
    
    def post(self, request, pk):
        scan_session = get_object_or_404(ScanSession, pk=pk)
        
        if scan_session.status in ['running', 'pending']:
            # Set is_cancelled flag - scan loop akan mendeteksi ini
            scan_session.cancel()
            
            ScanLog.objects.create(
                scan_session=scan_session,
                log_type='warning',
                message='Permintaan pembatalan dikirim'
            )
            messages.success(request, 'Scan sedang dibatalkan...')
        
        return redirect('detector:scan_list')


class ScanLogsView(View):
    """API endpoint untuk mengambil log scan real-time + deteksi"""
    
    def get(self, request, pk):
        scan_session = get_object_or_404(ScanSession, pk=pk)
        
        # Get last N logs (exclude detection warnings - those go to separate card)
        last_id = request.GET.get('last_id', 0)
        last_detection_id = request.GET.get('last_detection_id', 0)
        try:
            last_id = int(last_id)
            last_detection_id = int(last_detection_id)
        except:
            last_id = 0
            last_detection_id = 0
        
        # Filter out detection logs (yang ada 🚨) dari log biasa
        logs = ScanLog.objects.filter(
            scan_session=scan_session,
            id__gt=last_id
        ).exclude(
            message__contains='🚨'
        ).order_by('id')[:50]
        
        logs_data = [{
            'id': log.id,
            'type': log.log_type,
            'message': log.message,
            'url': log.url,
            'time': log.created_at.strftime('%H:%M:%S')
        } for log in logs]
        
        # Get new detections
        detections = DetectedContent.objects.filter(
            page__scan_session=scan_session,
            id__gt=last_detection_id
        ).select_related('page', 'keyword').order_by('id')[:20]
        
        # Map category to display name
        category_map = {
            'judol': 'Judi Online',
            'obat_penguat': 'Obat Penguat',
            'obat_aborsi': 'Obat Aborsi',
            'konten_dewasa': 'Konten Dewasa',
            'penipuan': 'Penipuan',
            'lainnya': 'Lainnya'
        }
        
        detections_data = [{
            'id': det.id,
            'url': det.page.url,
            'keyword': det.matched_text[:50],
            'category': category_map.get(det.category, det.category),
            'severity': det.severity,
            'confidence_score': det.confidence_score,
            'time': det.detected_at.strftime('%H:%M:%S')
        } for det in detections]
        
        return JsonResponse({
            'status': scan_session.status,
            'pages_scanned': scan_session.pages_scanned,
            'max_pages': scan_session.max_pages,
            'issues_found': scan_session.issues_found,
            'logs': logs_data,
            'detections': detections_data
        })


class ScanListView(ListView):
    """Daftar semua scan session"""
    model = ScanSession
    template_name = 'detector/scan_list.html'
    context_object_name = 'scans'
    paginate_by = 20
    ordering = ['-created_at']


class RunningScanListView(ListView):
    """Daftar scan yang sedang berjalan"""
    model = ScanSession
    template_name = 'detector/running_scans.html'
    context_object_name = 'scans'
    
    def get_queryset(self):
        return ScanSession.objects.filter(
            status__in=['running', 'pending']
        ).order_by('-created_at')


class DeleteScanView(View):
    """Hapus satu scan session beserta semua data terkait"""
    
    def post(self, request, pk):
        scan = get_object_or_404(ScanSession, pk=pk)
        target_url = scan.target_url
        
        # Hapus scan (cascade akan menghapus pages, detections, logs)
        scan.delete()
        
        messages.success(request, f'Scan "{target_url}" berhasil dihapus')
        return redirect('detector:scan_list')


class ClearScanHistoryView(View):
    """Hapus semua riwayat scan dan konten terdeteksi"""
    
    def post(self, request):
        # Hitung sebelum hapus
        scan_count = ScanSession.objects.count()
        detection_count = DetectedContent.objects.count()
        
        # Hapus semua (cascade akan menghapus pages, detections, logs)
        ScanSession.objects.all().delete()
        
        messages.success(
            request, 
            f'Berhasil menghapus {scan_count} sesi scan dan {detection_count} konten terdeteksi'
        )
        return redirect('detector:scan_list')


class ScanDetailView(DetailView):
    """Detail scan session"""
    model = ScanSession
    template_name = 'detector/scan_detail.html'
    context_object_name = 'scan'
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scan = self.object
        
        # Halaman yang terdeteksi konten negatif
        context['pages_with_issues'] = (
            scan.pages
            .annotate(detection_count=Count('detections'))
            .filter(detection_count__gt=0)
            .order_by('-detection_count')
        )
        
        # Halaman yang gagal/timeout
        context['failed_pages'] = (
            scan.pages
            .filter(status='failed')
            .order_by('-scraped_at')
        )
        
        # Semua deteksi dalam scan ini
        context['detections'] = (
            DetectedContent.objects
            .filter(page__scan_session=scan)
            .select_related('page', 'keyword')
            .order_by('-detected_at')
        )
        
        # Ringkasan per kategori
        context['category_summary'] = (
            DetectedContent.objects
            .filter(page__scan_session=scan)
            .values('category')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        
        # Stats
        context['total_success'] = scan.pages.filter(status='scraped').count()
        context['total_failed'] = scan.pages.filter(status='failed').count()
        
        return context


class DetectionListView(ListView):
    """Daftar semua konten terdeteksi"""
    model = DetectedContent
    template_name = 'detector/detection_list.html'
    context_object_name = 'detections'
    paginate_by = 50
    
    def get_queryset(self):
        queryset = DetectedContent.objects.select_related('page', 'keyword').order_by('-detected_at')
        
        # Exclude whitelisted items
        active_whitelists = Whitelist.objects.filter(is_active=True)
        
        # 1. URL Whitelist
        url_whitelist = active_whitelists.filter(whitelist_type='url').values_list('url', flat=True)
        if url_whitelist:
            queryset = queryset.exclude(page__url__in=url_whitelist)
            
        # 2. Domain Whitelist
        domain_whitelist = active_whitelists.filter(whitelist_type='domain').values_list('domain', flat=True)
        if domain_whitelist:
            queryset = queryset.exclude(page__domain__in=domain_whitelist)
            
        # 3. Keyword + URL Whitelist
        keyword_url_whitelists = active_whitelists.filter(whitelist_type='keyword_url')
        if keyword_url_whitelists.exists():
            q_objects = Q()
            for wl in keyword_url_whitelists:
                if wl.keyword and wl.url:
                    q_objects |= Q(matched_text__iexact=wl.keyword, page__url=wl.url)
            
            if q_objects:
                queryset = queryset.exclude(q_objects)
        
        # Filter berdasarkan kategori
        category = self.request.GET.get('category')
        if category:
            queryset = queryset.filter(category=category)
        
        # Filter berdasarkan status
        status = self.request.GET.get('status')
        if status == 'unresolved':
            queryset = queryset.filter(is_resolved=False)
        elif status == 'resolved':
            queryset = queryset.filter(is_resolved=True)
        elif status == 'reported':
            queryset = queryset.filter(is_reported=True)
        elif status == 'false_positive':
            queryset = queryset.filter(is_false_positive=True)
        
        # Filter berdasarkan severity
        severity = self.request.GET.get('severity')
        if severity:
            queryset = queryset.filter(severity=severity)
        
        # Filter berdasarkan confidence level
        confidence = self.request.GET.get('confidence')
        if confidence == 'high':
            queryset = queryset.filter(confidence_score__gte=0.8)
        elif confidence == 'medium':
            queryset = queryset.filter(confidence_score__gte=0.5, confidence_score__lt=0.8)
        elif confidence == 'low':
            queryset = queryset.filter(confidence_score__lt=0.5)
        
        # Sorting
        sort = self.request.GET.get('sort')
        if sort == 'confidence_asc':
            queryset = queryset.order_by('confidence_score')
        elif sort == 'confidence_desc':
            queryset = queryset.order_by('-confidence_score')
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = Keyword.CATEGORY_CHOICES
        context['current_category'] = self.request.GET.get('category', '')
        context['current_status'] = self.request.GET.get('status', '')
        context['current_severity'] = self.request.GET.get('severity', '')
        context['current_confidence'] = self.request.GET.get('confidence', '')
        context['current_sort'] = self.request.GET.get('sort', '')
        return context


class DetectionDetailView(DetailView):
    """Detail konten terdeteksi"""
    model = DetectedContent
    template_name = 'detector/detection_detail.html'
    context_object_name = 'detection'


class MarkReportedView(View):
    """Tandai deteksi sebagai sudah dilaporkan"""
    
    def post(self, request, pk):
        detection = get_object_or_404(DetectedContent, pk=pk)
        detection.mark_reported()
        messages.success(request, 'Konten berhasil ditandai sebagai dilaporkan')
        return redirect('detector:detection_detail', pk=pk)


class MarkResolvedView(View):
    """Tandai deteksi sebagai sudah ditangani"""
    
    def post(self, request, pk):
        detection = get_object_or_404(DetectedContent, pk=pk)
        detection.mark_resolved()
        messages.success(request, 'Konten berhasil ditandai sebagai sudah ditangani')
        return redirect('detector:detection_detail', pk=pk)


class BulkActionView(View):
    """Aksi bulk untuk deteksi"""
    
    def post(self, request):
        action = request.POST.get('action')
        detection_ids = request.POST.getlist('detection_ids')
        
        if not detection_ids:
            messages.error(request, 'Tidak ada item yang dipilih')
            return redirect('detector:detection_list')
        
        detections = DetectedContent.objects.filter(pk__in=detection_ids)
        
        if action == 'mark_reported':
            detections.update(is_reported=True, reported_at=timezone.now())
            messages.success(request, f'{len(detection_ids)} item ditandai sebagai dilaporkan')
        elif action == 'mark_resolved':
            detections.update(is_resolved=True, resolved_at=timezone.now())
            messages.success(request, f'{len(detection_ids)} item ditandai sebagai sudah ditangani')
        elif action == 'mark_false_positive':
            detections.update(is_false_positive=True)
            messages.success(request, f'{len(detection_ids)} item ditandai sebagai false positive')
        
        return redirect('detector:detection_list')


class ExportCSVView(View):
    """Export deteksi ke CSV"""
    
    def get(self, request):
        scan_id = request.GET.get('scan_id')
        category = request.GET.get('category')
        status = request.GET.get('status')
        
        queryset = DetectedContent.objects.select_related('page', 'keyword')
        
        if scan_id:
            queryset = queryset.filter(page__scan_session_id=scan_id)
        if category:
            queryset = queryset.filter(category=category)
        if status == 'unresolved':
            queryset = queryset.filter(is_resolved=False)
        elif status == 'resolved':
            queryset = queryset.filter(is_resolved=True)
        
        # Buat response CSV
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="deteksi_konten_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
        
        writer = csv.writer(response)
        writer.writerow([
            'ID', 'URL', 'Domain', 'Kata Kunci', 'Kategori', 'Tingkat', 
            'Lokasi', 'Konteks', 'Dilaporkan', 'Ditangani', 'Waktu Deteksi'
        ])
        
        for detection in queryset:
            writer.writerow([
                detection.id,
                detection.page.url,
                detection.page.domain,
                detection.matched_text,
                detection.get_category_display() if hasattr(detection, 'get_category_display') else detection.category,
                detection.severity,
                detection.location,
                detection.context[:200],
                'Ya' if detection.is_reported else 'Tidak',
                'Ya' if detection.is_resolved else 'Tidak',
                detection.detected_at.strftime('%Y-%m-%d %H:%M:%S')
            ])
        
        return response


class KeywordListView(ListView):
    """Daftar kata kunci"""
    model = Keyword
    template_name = 'detector/keyword_list.html'
    context_object_name = 'keywords'
    paginate_by = 50
    
    def get_queryset(self):
        queryset = Keyword.objects.all().order_by('category', 'keyword')
        category = self.request.GET.get('category')
        if category:
            queryset = queryset.filter(category=category)
        return queryset


class SeedKeywordsView(View):
    """Seed database dengan kata kunci default"""
    
    def post(self, request):
        detector = ContentDetector()
        created_count = 0
        
        severity_map = {
            'judol': 'high',
            'obat_penguat': 'medium',
            'obat_aborsi': 'high',
            'konten_dewasa': 'high',
            'penipuan': 'medium',
        }
        
        for category, keywords in detector.keywords.items():
            for keyword in keywords:
                _, created = Keyword.objects.get_or_create(
                    keyword=keyword,
                    defaults={
                        'category': category,
                        'severity': severity_map.get(category, 'medium'),
                        'is_active': True
                    }
                )
                if created:
                    created_count += 1
        
        messages.success(request, f'Berhasil menambahkan {created_count} kata kunci baru')
        return redirect('detector:keyword_list')


class WhitelistListView(ListView):
    """Daftar whitelist"""
    model = Whitelist
    template_name = 'detector/whitelist_list.html'
    context_object_name = 'whitelist_items'
    paginate_by = 50
    
    def get_queryset(self):
        queryset = Whitelist.objects.all().order_by('-created_at')
        whitelist_type = self.request.GET.get('type')
        if whitelist_type:
            queryset = queryset.filter(whitelist_type=whitelist_type)
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['type_choices'] = Whitelist.TYPE_CHOICES
        context['current_type'] = self.request.GET.get('type', '')
        return context


class WhitelistAddView(View):
    """Tambah whitelist baru"""
    
    def post(self, request):
        # Check if AJAX request (from run_scan.html)
        is_ajax = request.headers.get('Content-Type') == 'application/json'
        
        if is_ajax:
            try:
                data = json.loads(request.body)
                url = data.get('url', '').strip()
                keyword = data.get('keyword', '').strip()
                whitelist_type = data.get('whitelist_type', 'keyword_url')
                reason = data.get('reason', '')
            except json.JSONDecodeError:
                return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        else:
            url = request.POST.get('url', '').strip()
            keyword = request.POST.get('keyword', '').strip()
            whitelist_type = request.POST.get('whitelist_type', 'url')
            reason = request.POST.get('reason', '').strip()
        
        if not url:
            if is_ajax:
                return JsonResponse({'success': False, 'error': 'URL harus diisi'}, status=400)
            messages.error(request, 'URL harus diisi')
            return redirect('detector:whitelist_list')
        
        # Validasi URL
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Cek duplikat
        existing = Whitelist.objects.filter(url=url)
        if keyword and whitelist_type == 'keyword_url':
            existing = existing.filter(keyword__iexact=keyword)
        
        if existing.exists():
            if is_ajax:
                return JsonResponse({'success': False, 'error': 'URL sudah ada di whitelist'}, status=400)
            messages.warning(request, 'URL sudah ada di whitelist')
            return redirect('detector:whitelist_list')
        
        # Buat whitelist baru
        whitelist = Whitelist.objects.create(
            url=url,
            keyword=keyword if whitelist_type == 'keyword_url' else '',
            whitelist_type=whitelist_type,
            reason=reason,
            is_active=True
        )
        
        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': 'URL berhasil ditambahkan ke whitelist',
                'whitelist_id': whitelist.pk
            })
        
        messages.success(request, 'URL berhasil ditambahkan ke whitelist')
        return redirect('detector:whitelist_list')


class WhitelistEditView(View):
    """Edit whitelist"""
    
    def get(self, request, pk):
        whitelist = get_object_or_404(Whitelist, pk=pk)
        return JsonResponse({
            'id': whitelist.pk,
            'url': whitelist.url,
            'domain': whitelist.domain,
            'keyword': whitelist.keyword,
            'whitelist_type': whitelist.whitelist_type,
            'reason': whitelist.reason,
            'is_active': whitelist.is_active
        })
    
    def post(self, request, pk):
        whitelist = get_object_or_404(Whitelist, pk=pk)
        
        # Check if AJAX request
        is_ajax = request.headers.get('Content-Type') == 'application/json'
        
        if is_ajax:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
        else:
            data = request.POST
        
        url = data.get('url', '').strip()
        if url:
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            whitelist.url = url
        
        whitelist.keyword = data.get('keyword', '').strip()
        whitelist.whitelist_type = data.get('whitelist_type', whitelist.whitelist_type)
        whitelist.reason = data.get('reason', '').strip()
        
        if 'is_active' in data:
            whitelist.is_active = data.get('is_active') in [True, 'true', 'on', '1', 1]
        
        whitelist.save()
        
        if is_ajax:
            return JsonResponse({'success': True, 'message': 'Whitelist berhasil diperbarui'})
        
        messages.success(request, 'Whitelist berhasil diperbarui')
        return redirect('detector:whitelist_list')


class WhitelistDeleteView(View):
    """Hapus whitelist"""
    
    def post(self, request, pk):
        whitelist = get_object_or_404(Whitelist, pk=pk)
        
        # Check if AJAX request
        is_ajax = request.headers.get('Content-Type') == 'application/json' or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        whitelist.delete()
        
        if is_ajax:
            return JsonResponse({'success': True, 'message': 'Whitelist berhasil dihapus'})
        
        messages.success(request, 'Whitelist berhasil dihapus')
        return redirect('detector:whitelist_list')


class WhitelistToggleView(View):
    """Toggle status aktif whitelist"""
    
    def post(self, request, pk):
        whitelist = get_object_or_404(Whitelist, pk=pk)
        whitelist.is_active = not whitelist.is_active
        whitelist.save(update_fields=['is_active', 'updated_at'])
        
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        if is_ajax:
            return JsonResponse({
                'success': True,
                'is_active': whitelist.is_active,
                'message': f'Whitelist {"diaktifkan" if whitelist.is_active else "dinonaktifkan"}'
            })
        
        messages.success(request, f'Whitelist berhasil {"diaktifkan" if whitelist.is_active else "dinonaktifkan"}')
        return redirect('detector:whitelist_list')

