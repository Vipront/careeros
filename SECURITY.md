# Security / Güvenlik

Keep API keys, Telegram credentials, dashboard passwords and applicant data out of Git. `.env`, databases, generated documents, logs, backups and `vault/` are private runtime files.

Run the local demo on loopback. A remote dashboard needs authentication, HTTPS and reviewed network controls. Review application drafts and eligibility results before use.

This copy starts a new Git history without the private deployment's old Gitleaks exceptions. Revoke or rotate any credential previously committed; deleting a file does not remove earlier commits or copies.

Report vulnerabilities privately to the repository owner through their GitHub profile. Include the affected file and reproduction steps; keep working credentials and applicant data out of public issues.

---

API anahtarlarını, Telegram erişim bilgilerini, panel parolalarını ve aday verilerini Git'e ekleme. `.env`, veritabanları, üretilen belgeler, loglar, yedekler ve `vault/` özel çalışma dosyalarıdır.

Yerel demoyu yalnız bu bilgisayardan erişilebilecek şekilde çalıştır. Uzak panel için kimlik doğrulama, HTTPS ve incelenmiş ağ erişim kuralları gerekir. Başvuru taslaklarını ve uygunluk sonuçlarını kullanmadan önce incele.

Bu kopya özel kurulumun eski Gitleaks istisnalarını içermeyen yeni bir Git geçmişiyle başlar. Daha önce commit edilmiş erişim bilgisini iptal et veya yenile. Dosyayı silmek önceki commit'leri ve kopyaları kaldırmaz.

Güvenlik açığını repo sahibine GitHub profili üzerinden özel olarak bildir. Etkilenen dosyayı ve tekrar üretme adımlarını ekle; çalışan erişim bilgilerini ve aday verilerini herkese açık issue'lara yazma.
