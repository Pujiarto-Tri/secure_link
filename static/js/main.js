/**
 * Content Guardian - Main JavaScript
 */

document.addEventListener('DOMContentLoaded', function () {
    // Initialize tooltips
    const tooltipTriggerList = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    const tooltipList = [...tooltipTriggerList].map(tooltipTriggerEl => new bootstrap.Tooltip(tooltipTriggerEl));

    // Search functionality
    const searchInput = document.querySelector('.search-input');
    if (searchInput) {
        searchInput.addEventListener('keypress', function (e) {
            if (e.key === 'Enter') {
                const query = this.value.trim();
                if (query) {
                    // Redirect to detection list with search
                    window.location.href = `/detections/?search=${encodeURIComponent(query)}`;
                }
            }
        });
    }

    // Auto-dismiss alerts
    const alerts = document.querySelectorAll('.alert:not(.alert-permanent)');
    alerts.forEach(alert => {
        setTimeout(() => {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        }, 5000);
    });

    // Confirm dangerous actions
    const dangerButtons = document.querySelectorAll('[data-confirm]');
    dangerButtons.forEach(button => {
        button.addEventListener('click', function (e) {
            if (!confirm(this.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });
});

// Helper function to format numbers
function formatNumber(num) {
    return new Intl.NumberFormat('id-ID').format(num);
}

// Helper function for time ago
function timeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);

    let interval = seconds / 31536000;
    if (interval > 1) return Math.floor(interval) + ' tahun lalu';

    interval = seconds / 2592000;
    if (interval > 1) return Math.floor(interval) + ' bulan lalu';

    interval = seconds / 86400;
    if (interval > 1) return Math.floor(interval) + ' hari lalu';

    interval = seconds / 3600;
    if (interval > 1) return Math.floor(interval) + ' jam lalu';

    interval = seconds / 60;
    if (interval > 1) return Math.floor(interval) + ' menit lalu';

    return 'baru saja';
}
