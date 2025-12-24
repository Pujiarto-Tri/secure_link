from django.db import models
from django.utils import timezone


class Keyword(models.Model):
    """Kata kunci untuk deteksi konten negatif"""
    
    CATEGORY_CHOICES = [
        ('judol', 'Judi Online'),
        ('obat_penguat', 'Obat Penguat'),
        ('obat_aborsi', 'Obat Aborsi'),
        ('konten_dewasa', 'Konten Dewasa'),
        ('penipuan', 'Penipuan'),
        ('lainnya', 'Lainnya'),
    ]
    
    SEVERITY_CHOICES = [
        ('low', 'Rendah'),
        ('medium', 'Sedang'),
        ('high', 'Tinggi'),
    ]
    
    keyword = models.CharField(max_length=200, verbose_name='Kata Kunci')
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='lainnya', verbose_name='Kategori')
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default='medium', verbose_name='Tingkat Keparahan')
    is_active = models.BooleanField(default=True, verbose_name='Aktif')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Kata Kunci'
        verbose_name_plural = 'Kata Kunci'
        ordering = ['category', 'keyword']
    
    def __str__(self):
        return f"{self.keyword} ({self.get_category_display()})"


class ScanSession(models.Model):
    """Sesi pemindaian website"""
    
    STATUS_CHOICES = [
        ('pending', 'Menunggu'),
        ('running', 'Berjalan'),
        ('completed', 'Selesai'),
        ('failed', 'Gagal'),
        ('cancelled', 'Dibatalkan'),
    ]
    
    target_url = models.URLField(verbose_name='URL Target')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='Status')
    started_at = models.DateTimeField(null=True, blank=True, verbose_name='Waktu Mulai')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='Waktu Selesai')
    pages_scanned = models.IntegerField(default=0, verbose_name='Halaman Dipindai')
    issues_found = models.IntegerField(default=0, verbose_name='Masalah Ditemukan')
    error_message = models.TextField(blank=True, verbose_name='Pesan Error')
    max_pages = models.IntegerField(default=100, verbose_name='Maksimum Halaman')
    scan_subdomains = models.BooleanField(default=True, verbose_name='Pindai Subdomain')
    is_cancelled = models.BooleanField(default=False, verbose_name='Dibatalkan')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = 'Sesi Pemindaian'
        verbose_name_plural = 'Sesi Pemindaian'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Scan {self.target_url} - {self.get_status_display()}"
    
    def start(self):
        self.status = 'running'
        self.started_at = timezone.now()
        self.is_cancelled = False
        self.save()
    
    def complete(self):
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.save()
    
    def fail(self, error_message=''):
        self.status = 'failed'
        self.completed_at = timezone.now()
        self.error_message = error_message
        self.save()
    
    def cancel(self):
        """Tandai scan untuk dibatalkan"""
        self.is_cancelled = True
        self.status = 'cancelled'
        self.completed_at = timezone.now()
        self.error_message = 'Dibatalkan oleh pengguna'
        self.save()
    
    def should_stop(self):
        """Cek apakah scan harus dihentikan (dipanggil dari loop scan)"""
        # Refresh dari database untuk dapat nilai terbaru
        self.refresh_from_db(fields=['is_cancelled'])
        return self.is_cancelled


class ScrapedPage(models.Model):
    """Halaman yang sudah di-scrape"""
    
    STATUS_CHOICES = [
        ('pending', 'Menunggu'),
        ('scraped', 'Berhasil'),
        ('failed', 'Gagal'),
    ]
    
    scan_session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='pages', verbose_name='Sesi Pemindaian')
    url = models.URLField(max_length=500, verbose_name='URL')
    domain = models.CharField(max_length=200, verbose_name='Domain')
    title = models.CharField(max_length=500, blank=True, verbose_name='Judul')
    meta_description = models.TextField(blank=True, verbose_name='Meta Description')
    content = models.TextField(blank=True, verbose_name='Konten')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='Status')
    http_status = models.IntegerField(null=True, blank=True, verbose_name='HTTP Status')
    error_message = models.TextField(blank=True, verbose_name='Pesan Error')
    scraped_at = models.DateTimeField(null=True, blank=True, verbose_name='Waktu Scrape')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = 'Halaman Terscrape'
        verbose_name_plural = 'Halaman Terscrape'
        ordering = ['-scraped_at']
        unique_together = ['scan_session', 'url']
    
    def __str__(self):
        return self.title or self.url


class DetectedContent(models.Model):
    """Konten negatif yang terdeteksi"""
    
    page = models.ForeignKey(ScrapedPage, on_delete=models.CASCADE, related_name='detections', verbose_name='Halaman')
    keyword = models.ForeignKey(Keyword, on_delete=models.SET_NULL, null=True, related_name='detections', verbose_name='Kata Kunci')
    matched_text = models.CharField(max_length=200, verbose_name='Teks Tercocok')
    context = models.TextField(verbose_name='Konteks')
    category = models.CharField(max_length=50, verbose_name='Kategori')
    severity = models.CharField(max_length=20, default='medium', verbose_name='Tingkat Keparahan')
    location = models.CharField(max_length=50, default='content', verbose_name='Lokasi')  # title, meta, content
    
    # Confidence scoring untuk mengurangi false positive
    confidence_score = models.FloatField(default=1.0, verbose_name='Skor Kepercayaan')
    is_false_positive = models.BooleanField(default=False, verbose_name='False Positive')
    safe_context_found = models.TextField(blank=True, verbose_name='Konteks Aman Ditemukan')
    
    is_reported = models.BooleanField(default=False, verbose_name='Sudah Dilaporkan')
    is_resolved = models.BooleanField(default=False, verbose_name='Sudah Ditangani')
    notes = models.TextField(blank=True, verbose_name='Catatan')
    detected_at = models.DateTimeField(auto_now_add=True, verbose_name='Waktu Deteksi')
    reported_at = models.DateTimeField(null=True, blank=True, verbose_name='Waktu Dilaporkan')
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='Waktu Ditangani')
    
    class Meta:
        verbose_name = 'Konten Terdeteksi'
        verbose_name_plural = 'Konten Terdeteksi'
        ordering = ['-detected_at']
    
    def __str__(self):
        return f"{self.matched_text} di {self.page.url}"
    
    def mark_reported(self):
        self.is_reported = True
        self.reported_at = timezone.now()
        self.save()
    
    def mark_resolved(self):
        self.is_resolved = True
        self.resolved_at = timezone.now()
        self.save()


class ScanLog(models.Model):
    """Log real-time aktivitas scan"""
    
    LOG_TYPE_CHOICES = [
        ('info', 'Info'),
        ('success', 'Berhasil'),
        ('warning', 'Peringatan'),
        ('error', 'Error'),
    ]
    
    scan_session = models.ForeignKey(ScanSession, on_delete=models.CASCADE, related_name='logs')
    log_type = models.CharField(max_length=20, choices=LOG_TYPE_CHOICES, default='info')
    message = models.TextField()
    url = models.URLField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = 'Log Scan'
        verbose_name_plural = 'Log Scan'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"[{self.log_type}] {self.message[:50]}"


class Whitelist(models.Model):
    """URL yang dikecualikan dari deteksi (untuk menghindari false positive)"""
    
    TYPE_CHOICES = [
        ('url', 'URL Spesifik'),
        ('domain', 'Domain'),
        ('keyword_url', 'Keyword + URL'),
    ]
    
    url = models.URLField(max_length=500, verbose_name='URL')
    domain = models.CharField(max_length=200, blank=True, verbose_name='Domain')
    keyword = models.CharField(max_length=200, blank=True, verbose_name='Keyword (opsional)')
    whitelist_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='url', verbose_name='Tipe Whitelist')
    reason = models.TextField(blank=True, verbose_name='Alasan Whitelist')
    is_active = models.BooleanField(default=True, verbose_name='Aktif')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Whitelist'
        verbose_name_plural = 'Whitelist'
        ordering = ['-created_at']
    
    def __str__(self):
        if self.whitelist_type == 'domain':
            return f"Domain: {self.domain}"
        elif self.whitelist_type == 'keyword_url':
            return f"{self.keyword} di {self.url[:50]}"
        return self.url[:50]
    
    def save(self, *args, **kwargs):
        # Auto-extract domain from URL if not provided
        if not self.domain and self.url:
            from urllib.parse import urlparse
            parsed = urlparse(self.url)
            self.domain = parsed.netloc
        super().save(*args, **kwargs)

