from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from detector.models import ScanLog

class Command(BaseCommand):
    help = 'Prune scan logs older than N days to optimize SQLite performance'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Keep logs for this many days (default: 30)'
        )

    def handle(self, *args, **options):
        days = options['days']
        cutoff_date = timezone.now() - timedelta(days=days)
        
        self.stdout.write(f"Pruning ScanLog entries created before {cutoff_date}...")
        
        # We can run a delete query on ScanLog table
        deleted_count, _ = ScanLog.objects.filter(created_at__lt=cutoff_date).delete()
        
        self.stdout.write(self.style.SUCCESS(f"Successfully pruned {deleted_count} scan logs older than {days} days."))
