from django.urls import path
from . import views

app_name = 'detector'

urlpatterns = [
    # Dashboard
    path('', views.DashboardView.as_view(), name='dashboard'),
    
    # Scanning
    path('scan/start/', views.StartScanView.as_view(), name='start_scan'),
    path('scan/<int:pk>/run/', views.RunScanView.as_view(), name='run_scan'),
    path('scan/<int:pk>/cancel/', views.CancelScanView.as_view(), name='cancel_scan'),
    path('scan/<int:pk>/logs/', views.ScanLogsView.as_view(), name='scan_logs'),
    path('scan/', views.ScanListView.as_view(), name='scan_list'),
    path('scan/<int:pk>/', views.ScanDetailView.as_view(), name='scan_detail'),
    
    # Detections
    path('detections/', views.DetectionListView.as_view(), name='detection_list'),
    path('detections/<int:pk>/', views.DetectionDetailView.as_view(), name='detection_detail'),
    path('detections/<int:pk>/report/', views.MarkReportedView.as_view(), name='mark_reported'),
    path('detections/<int:pk>/resolve/', views.MarkResolvedView.as_view(), name='mark_resolved'),
    path('detections/bulk-action/', views.BulkActionView.as_view(), name='bulk_action'),
    
    # Export
    path('export/csv/', views.ExportCSVView.as_view(), name='export_csv'),
    
    # Keywords
    path('keywords/', views.KeywordListView.as_view(), name='keyword_list'),
    path('keywords/seed/', views.SeedKeywordsView.as_view(), name='seed_keywords'),
    
    # Whitelist
    path('whitelist/', views.WhitelistListView.as_view(), name='whitelist_list'),
    path('whitelist/add/', views.WhitelistAddView.as_view(), name='whitelist_add'),
    path('whitelist/<int:pk>/edit/', views.WhitelistEditView.as_view(), name='whitelist_edit'),
    path('whitelist/<int:pk>/delete/', views.WhitelistDeleteView.as_view(), name='whitelist_delete'),
    path('whitelist/<int:pk>/toggle/', views.WhitelistToggleView.as_view(), name='whitelist_toggle'),
]

