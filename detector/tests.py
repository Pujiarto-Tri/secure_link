"""
Unit tests for Content Detection with Confidence Scoring
"""
from django.test import TestCase
from detector.detection import ContentDetector


class ConfidenceScoreTestCase(TestCase):
    """Test cases for the confidence scoring system"""
    
    def setUp(self):
        self.detector = ContentDetector()
    
    def test_safe_context_lowers_score(self):
        """
        Konten dengan kata negatif DAN kata safe context
        harus punya confidence score lebih rendah
        """
        # Konten puskesmas dengan kata "misoprostol" (kategori obat_aborsi)
        title = "Penyuluhan Kesehatan Ibu dan Anak - Puskesmas Sejahtera"
        meta = "Puskesmas Sejahtera memberikan edukasi tentang penggunaan misoprostol dan kandungan bagi ibu hamil"
        content = "Tim dokter dari Puskesmas Sejahtera mengadakan penyuluhan kesehatan reproduksi untuk ibu-ibu di desa. Materi yang disampaikan meliputi efek samping misoprostol dan cara menjaga kehamilan."
        url = "https://puskesmassejahtera.go.id/berita/penyuluhan"
        
        detections, score, safe_context = self.detector.detect_in_sections(title, meta, content, url)
        
        # Harus ada deteksi (kata "rahim")
        self.assertGreater(len(detections), 0, "Harus ada minimal 1 deteksi")
        
        # Score harus rendah karena banyak safe context
        self.assertLess(score, 0.5, f"Score {score} harus < 0.5 karena banyak safe context")
        
        # Safe context harus ditemukan
        self.assertGreater(len(safe_context), 0, "Safe context harus ditemukan")
        
        print(f"✓ Safe context test passed: score={score}, safe_context={safe_context}")
    
    def test_negative_content_high_score(self):
        """
        Konten judi asli harus punya confidence score tinggi
        """
        title = "Slot Gacor Maxwin Hari Ini - Daftar Sekarang!"
        meta = "Main slot online gacor dengan RTP tertinggi. Bonus new member 100%, withdraw cepat."
        content = "Agen slot online terpercaya dengan berbagai permainan dari Pragmatic. Daftar slot gratis dan dapatkan bonus deposit 100%. Slot gacor hari ini dengan RTP live tertinggi. Gates of Olympus, Sweet Bonanza, Mahjong Ways semua tersedia!"
        url = "https://slot-gacor123.com"
        
        detections, score, safe_context = self.detector.detect_in_sections(title, meta, content, url)
        
        # Harus ada banyak deteksi
        self.assertGreater(len(detections), 5, "Harus ada banyak deteksi konten judi")
        
        # Score harus tinggi karena ini judi asli
        self.assertGreaterEqual(score, 0.8, f"Score {score} harus >= 0.8 untuk konten judi asli")
        
        # Safe context seharusnya tidak ada atau minimal
        self.assertLessEqual(len(safe_context), 1, "Safe context seharusnya minimal")
        
        print(f"✓ Negative content test passed: score={score}, detections={len(detections)}")
    
    def test_mtq_content_lower_score(self):
        """
        Konten MTQ dengan kata yang mungkin false positive
        harus punya score lebih rendah
        """
        title = "MTQ ke-XXX Tingkat Provinsi NTB Resmi Dibuka"
        meta = "Musabaqah Tilawatil Quran tingkat provinsi NTB digelar di Mataram dengan peserta dari seluruh kabupaten"
        content = "Gubernur NTB membuka acara MTQ tingkat provinsi. Peserta qori dan qoriah dari berbagai kabupaten berkompetisi dalam tilawah Quran. Acara islami ini diselenggarakan di Masjid Agung."
        url = "https://ntbprov.go.id/berita/mtq"
        
        detections, score, safe_context = self.detector.detect_in_sections(title, meta, content, url)
        
        # Jika ada deteksi, score harus rendah karena konteks religius
        if len(detections) > 0:
            self.assertLess(score, 0.5, f"Score {score} harus < 0.5 untuk konten MTQ")
            self.assertGreater(len(safe_context), 0, "Safe context religius harus ditemukan")
        
        print(f"✓ MTQ content test passed: score={score}, safe_context={safe_context}")
    
    def test_title_location_increases_score(self):
        """
        Keyword di title harus meningkatkan score
        """
        # Keyword di title
        title1 = "Slot Gacor Hari Ini"
        meta1 = ""
        content1 = "Selamat datang"
        
        detections1, score1, _ = self.detector.detect_in_sections(title1, meta1, content1)
        
        # Keyword di content saja
        title2 = ""
        meta2 = ""
        content2 = "Slot gacor hari ini banyak tersedia"
        
        detections2, score2, _ = self.detector.detect_in_sections(title2, meta2, content2)
        
        # Score1 harus lebih tinggi karena di title
        if len(detections1) > 0 and len(detections2) > 0:
            self.assertGreaterEqual(score1, score2, 
                f"Score di title ({score1}) harus >= score di content ({score2})")
        
        print(f"✓ Title location test passed: title_score={score1}, content_score={score2}")
        
    def test_outbound_links_boost_score(self):
        """
        Tautan keluar mencurigakan di domain .go.id harus menaikkan score ke 0.98+
        """
        title = "Berita Terbaru"
        meta = "Dapatkan berita terbaru di situs kami"
        content = "Silakan klik link alternatif slot gacor ini untuk bermain judi online terpercaya."
        url = "https://lombokbaratkab.go.id/berita/1"
        outbound = ["https://judi-slot-online.xyz/register"]
        
        detections, score, safe_context = self.detector.detect_in_sections(title, meta, content, url, outbound_links=outbound)
        
        self.assertGreater(len(detections), 0)
        self.assertGreaterEqual(score, 0.95, f"Score {score} harus >= 0.95 karena ada outbound link mencurigakan di .go.id")
        print(f"✓ Outbound link boost test passed: score={score}")

    def test_keyword_density_lowers_score(self):
        """
        Kerapatan kata kunci yang rendah (hanya 1 keyword di teks sangat panjang) harus menurunkan score
        """
        title = "Pengumuman Resmi"
        meta = "Pengumuman hasil seleksi administrasi"
        # Teks panjang > 1000 karakter, hanya 1 keyword "slot"
        content = "Ini adalah pengumuman resmi hasil seleksi administrasi pegawai pemerintah. " + "a " * 800 + " slot " + "b " * 800
        url = "https://lombokbaratkab.go.id/pengumuman"
        
        detections, score, _ = self.detector.detect_in_sections(title, meta, content, url)
        self.assertEqual(len(detections), 1)
        self.assertLess(score, 0.50, f"Score {score} harus < 0.50 karena kepadatan sangat rendah")
        print(f"✓ Keyword density dampening test passed: score={score}")

    def test_keyword_diversity_increases_score(self):
        """
        Keberagaman kata kunci negatif dalam satu kategori harus menaikkan score
        """
        title = "Daftar Akun"
        meta = "Main judi online"
        content = "Daftar slot online dengan bandar togel terpercaya judi online maxwin hari ini."
        
        detections, score, _ = self.detector.detect_in_sections(title, meta, content)
        
        # Keunikan keyword negatif cukup tinggi (slot, judi online, bandar togel, maxwin)
        self.assertGreaterEqual(score, 0.85, f"Score {score} harus tinggi karena diversitas kata kunci negatif tinggi")
        print(f"✓ Keyword diversity test passed: score={score}")

    def test_proximity_clustering_increases_score(self):
        """
        Clustering kata kunci yang berdekatan harus menaikkan score
        """
        title = "Promo"
        meta = "Promo slot"
        # 3 hits berturut-turut berdekatan
        content = "Kami menawarkan slot online terpercaya judi online slot gacor!"
        
        detections, score, _ = self.detector.detect_in_sections(title, meta, content)
        self.assertGreaterEqual(score, 0.85, f"Score {score} harus tinggi karena clustering")
        print(f"✓ Proximity clustering test passed: score={score}")


class SafeContextKeywordsTestCase(TestCase):
    """Test cases for safe context keywords detection"""
    
    def setUp(self):
        self.detector = ContentDetector()
    
    def test_find_safe_context_health(self):
        """Test mencari safe context untuk kategori kesehatan"""
        text = "Dokter di puskesmas memberikan edukasi kesehatan untuk ibu hamil"
        safe_words = self.detector.find_safe_context(text, 'obat_aborsi')
        
        self.assertIn('dokter', safe_words)
        self.assertIn('puskesmas', safe_words)
        self.assertIn('edukasi', safe_words)
        
        print(f"✓ Health safe context test passed: found {safe_words}")
    
    def test_find_safe_context_religious(self):
        """Test mencari safe context untuk kategori keagamaan"""
        text = "Acara MTQ di masjid dengan pembacaan tilawah Quran oleh para qori"
        safe_words = self.detector.find_safe_context(text, 'konten_dewasa')
        
        self.assertIn('mtq', safe_words)
        self.assertIn('masjid', safe_words)
        self.assertIn('quran', safe_words)
        
        print(f"✓ Religious safe context test passed: found {safe_words}")
