# Yayın doğrulaması

[English](VALIDATION.md) | [Türkçe](VALIDATION.tr.md)

## SQLite güncellemesi — 1 Kasım 2026

Veritabanı artık yalnızca SQLite kullanıyor. Uzak istemci kodu, ayarları ve paket
bağımlılığı kaldırıldı. Otomatik yedeklerde yalnızca son başarılı kopya tutuluyor;
yeni yedek alınamazsa önceki korunuyor. Manuel yedekler saklanıyor.

Bu temiz yayın kopyasında veritabanı, yedekleme ve geri yükleme, işlem bütünlüğü,
inceleme, denetim, akış ve önbellekle ilgili 41 test geçti. Ruff `src`, `scripts`
ve `tests` için geçti; çevrimdışı JSON demosu çalıştı; Gitleaks bulgu bildirmedi.
Bu güncellemede tam regresyon paketi yeniden çalıştırılmadı.

Ek düzeltme, süresi dolmuş “ilan ayrıntısı bulunamadı” önbellek kayıtlarını aynı
geçmiş tarihle tekrarlamak yerine yeniden deniyor. Geleceğe ertelenmiş ve denemesi
sonlandırılmış kayıtlar korunuyor. Temiz yayın kopyasında regresyon senaryoları,
ilgili HTTP güvenliği ve akış kontrolleri dahil 28 test geçti.

## Önceki doğrulama

Temiz kopya 9 Ekim 2026'da (Europe/Istanbul) doğrulandı:

- Çevrimdışı regresyon paketi: 411 test geçti, 7 test atlandı, 7 alt test geçti. Altı uyarı Pydantic'in kullanımdan kaldırılan `.dict()` API'siyle ilgili.
- Ruff: `src`, `tests` ve `scripts/demo.py` için geçti.
- Mypy: `src/final_ranking.py` için geçti.
- Standart kütüphaneli JSON demosu ve mevcut dosyanın üzerine yazmayan yerel SQLite veri ekleme: geçti.
- Yeni sanal ortamda `pip install -r requirements-demo.txt`, demo verisi ekleme ve üç panel satırını yükleme, tam geliştirme bağımlılıkları olmadan geçti.
- Yerel Streamlit girişi, ilan listesi ve seçili ilan görünümü üç kurgu ilanla kontrol edildi. Görüntü: `demo-dashboard.png`.
- Gitleaks v8.30.1: temiz kaynakta bulgu yok. Varsayılan kurallar kullanıldı; özel geçmişin commit istisnası taşınmadı.

Özgün repo geçmişinde bir JWT bulgusu ve eski aday belgeleri var. Bunlar bu kopyaya dahil edilmedi. Tarama sonucu eski bir erişim bilgisinin iptal edildiğini kanıtlamaz. Özgün uzak repo bağlantısı sahibinin isteğiyle kaldırıldı.

Windows sandbox'taki ilk denemeler pytest geçici klasörlerini oluşturamadı. Son test, kullanıcının geçici klasöründeki ayrı kopyada çalıştırıldı; test fixture'ları ağ erişimini kapattı. Canlı akış veya ücretli model değerlendirmesi tetiklenmedi.

Altı sentetik karşılaştırma kaydı korundu. Özel üretim örnekleri ve onlara bağlı dört veri sözleşmesi testi çıkarıldı. Kurum adları ve aday profili kurgudur. Bu sonuçlar üretim öneri doğruluğunu göstermez.
