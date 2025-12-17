from django.contrib import admin
from .models import Keyword, ScanSession, ScrapedPage, DetectedContent


@admin.register(Keyword)
class KeywordAdmin(admin.ModelAdmin):
    list_display = ['keyword', 'category', 'severity', 'is_active', 'created_at']
    list_filter = ['category', 'severity', 'is_active']
    search_fields = ['keyword']
    list_editable = ['is_active', 'severity']
    ordering = ['category', 'keyword']


@admin.register(ScanSession)
class ScanSessionAdmin(admin.ModelAdmin):
    list_display = ['target_url', 'status', 'pages_scanned', 'issues_found', 'started_at', 'completed_at']
    list_filter = ['status']
    search_fields = ['target_url']
    readonly_fields = ['started_at', 'completed_at', 'pages_scanned', 'issues_found']
    ordering = ['-created_at']


@admin.register(ScrapedPage)
class ScrapedPageAdmin(admin.ModelAdmin):
    list_display = ['url', 'domain', 'status', 'http_status', 'scraped_at']
    list_filter = ['status', 'domain']
    search_fields = ['url', 'title']
    readonly_fields = ['scraped_at']
    ordering = ['-scraped_at']


@admin.register(DetectedContent)
class DetectedContentAdmin(admin.ModelAdmin):
    list_display = ['matched_text', 'category', 'severity', 'location', 'is_reported', 'is_resolved', 'detected_at']
    list_filter = ['category', 'severity', 'is_reported', 'is_resolved', 'location']
    search_fields = ['matched_text', 'context', 'page__url']
    list_editable = ['is_reported', 'is_resolved']
    readonly_fields = ['detected_at', 'reported_at', 'resolved_at']
    ordering = ['-detected_at']
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('page', 'keyword')
