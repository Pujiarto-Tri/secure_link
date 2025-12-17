"""
Views for the detector application
"""
import csv
import json
import logging
from io import StringIO
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.generic import ListView, DetailView, TemplateView
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.db.models import Count, Q

from .models import Keyword, ScanSession, ScrapedPage, DetectedContent, ScanLog
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
            
            # Inisialisasi scraper - TELITI (timeout 30s, sequential untuk stabilitas)
            scraper = WebScraper(
                base_domain=domain,
                delay=0.2,
                timeout=30,  # 30 detik untuk website lambat
                max_pages=scan_session.max_pages,
                scan_subdomains=scan_session.scan_subdomains,
                max_workers=3  # 3 concurrent untuk stabilitas
            )
            
            # Callback untuk logging setiap halaman + cek pembatalan
            def log_progress(result, pages_scraped, max_pages):
                # Cek apakah scan dibatalkan
                if scan_session.should_stop():
                    ScanLog.objects.create(
                        scan_session=scan_session,
                        log_type='warning',
                        message='Scan dibatalkan oleh pengguna'
                    )
                    scraper.cancel()  # Stop the scraper
                    return
                
                url = result.get('url', '')
                if result.get('success'):
                    ScanLog.objects.create(
                        scan_session=scan_session,
                        log_type='success',
                        message=f'[{pages_scraped}/{max_pages}] Berhasil: {url[:80]}',
                        url=url
                    )
                else:
                    # Tampilkan URL yang gagal beserta error-nya
                    error_msg = result.get('error', 'Unknown error')
                    ScanLog.objects.create(
                        scan_session=scan_session,
                        log_type='error',
                        message=f'[{pages_scraped}/{max_pages}] Gagal: {url[:60]} - {error_msg}',
                        url=url
                    )
                # Update progress di database
                scan_session.pages_scanned = pages_scraped
                scan_session.save(update_fields=['pages_scanned'])
            
            # Jalankan crawling dengan callback
            results = scraper.crawl(scan_session.target_url, callback=log_progress)
            
            ScanLog.objects.create(
                scan_session=scan_session,
                log_type='info',
                message=f'Crawling selesai. Memproses {len(results)} halaman...'
            )
            
            # Inisialisasi detector
            detector = ContentDetector()
            
            # Load keywords dari database
            active_keywords = Keyword.objects.filter(is_active=True)
            detector.load_keywords_from_db(active_keywords)
            
            total_issues = 0
            
            # Proses setiap halaman
            for result in results:
                # Simpan halaman
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
                
                if result.get('success'):
                    # Deteksi konten negatif
                    detections = detector.detect_in_sections(
                        title=result.get('title', ''),
                        meta_description=result.get('meta_description', ''),
                        content=result.get('content', '')
                    )
                    
                    # Simpan hasil deteksi
                    for detection in detections:
                        keyword_obj = Keyword.objects.filter(
                            keyword__iexact=detection['keyword'],
                            is_active=True
                        ).first()
                        
                        DetectedContent.objects.create(
                            page=page,
                            keyword=keyword_obj,
                            matched_text=detection['matched_text'][:200],
                            context=detection['context'][:1000],
                            category=detection['category'],
                            severity=detection.get('severity', 'medium'),
                            location=detection.get('location', 'content')
                        )
                        total_issues += 1
                        
                        # Log deteksi
                        ScanLog.objects.create(
                            scan_session=scan_session,
                            log_type='warning',
                            message=f'TERDETEKSI: "{detection["matched_text"]}" ({detection["category"]})',
                            url=result['url']
                        )
            
            # Update scan session
            scan_session.pages_scanned = len(results)
            scan_session.issues_found = total_issues
            scan_session.complete()
            
            ScanLog.objects.create(
                scan_session=scan_session,
                log_type='success',
                message=f'Scan selesai! {len(results)} halaman, {total_issues} konten negatif'
            )
            
            return JsonResponse({
                'success': True,
                'pages_scanned': len(results),
                'issues_found': total_issues,
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
    """API endpoint untuk mengambil log scan real-time"""
    
    def get(self, request, pk):
        scan_session = get_object_or_404(ScanSession, pk=pk)
        
        # Get last N logs
        last_id = request.GET.get('last_id', 0)
        try:
            last_id = int(last_id)
        except:
            last_id = 0
        
        logs = ScanLog.objects.filter(
            scan_session=scan_session,
            id__gt=last_id
        ).order_by('id')[:50]
        
        logs_data = [{
            'id': log.id,
            'type': log.log_type,
            'message': log.message,
            'url': log.url,
            'time': log.created_at.strftime('%H:%M:%S')
        } for log in logs]
        
        return JsonResponse({
            'status': scan_session.status,
            'pages_scanned': scan_session.pages_scanned,
            'max_pages': scan_session.max_pages,
            'logs': logs_data
        })


class ScanListView(ListView):
    """Daftar semua scan session"""
    model = ScanSession
    template_name = 'detector/scan_list.html'
    context_object_name = 'scans'
    paginate_by = 20
    ordering = ['-created_at']


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
        
        # Filter berdasarkan severity
        severity = self.request.GET.get('severity')
        if severity:
            queryset = queryset.filter(severity=severity)
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = Keyword.CATEGORY_CHOICES
        context['current_category'] = self.request.GET.get('category', '')
        context['current_status'] = self.request.GET.get('status', '')
        context['current_severity'] = self.request.GET.get('severity', '')
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
