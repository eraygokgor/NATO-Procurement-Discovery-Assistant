# NATO Procurement Discovery Assistant

NATO ihale ilanlarını toplayan, ek belgeleri metne dönüştüren ve yapılandırılmış
LLM değerlendirmelerini Excel'e aktaran Python uygulaması. NATO ile resmî bir
bağlantısı yoktur. Örnek değerlendirme profili herhangi bir şirketi temsil etmez.
Sonuçlar ön inceleme içindir; kaynak belgeler ve katılım koşulları insan tarafından
doğrulanmalıdır.

## İşleyiş

1. NATO fırsat listesini seçilen kaynaklar ve tarih aralığı için alır.
2. Desteklenen kaynaklarda detayları ve ekleri toplar; gerektiğinde Edge kullanır.
3. Belge metinlerini, kullanıcı tarafından düzenlenebilen ilgi profiliyle değerlendirir.
4. Sonuçları JSONL önbelleğine ve Türkçe başlıklı Excel raporuna kaydeder.
5. İsteğe bağlı olarak Teams Workflow üzerinden özet ve Excel gönderir.

Kaynaklar: NSPA, NCIA, ACT, IS, IMS ve ACO. Detay erişimi kaynaklara göre değişir;
IS/IMS mevcut uygulamada liste bilgisiyle değerlendirilir. Sitelerin erişim
kısıtlamaları nedeniyle her ilan veya ekin alınması garanti edilmez.

## Kurulum

Test edilen Python sürümü 3.12’dir; yeni ortam için Python 3.12 kullanın. Mevcut requirements.txt, gönderilen ortamın paket sürümlerini
korur; temiz ortamda bu sürümlerin kurulabilirliği ayrıca doğrulanmalıdır.
Eski .doc dosyalarının okunması Windows ve Microsoft Word gerektirir.
Tarayıcı fallback için Microsoft Edge gerekir. pywin32 yalnız Windows üzerinde kurulur.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Yerel `.env` dosyasında `LLM_URL`, `LLM_MODEL` ve `LLM_API_KEY` değerlerini doldurun.
LLM_URL, sağlayıcının OpenAI uyumlu API temel adresidir. Sağlayıcı Chat Completions,
JSON Schema structured output ve kodda kullanılan max_tokens parametresini
desteklemelidir. Her OpenAI uyumlu sunucu/model otomatik olarak uyumlu değildir.
Gerçek bağlantı adresleri ve anahtarları repoya eklemeyin.

```powershell
python main.py
```

## Prompt ve kategoriler

`prompts/system_prompt_current.txt` genel bir teknoloji ilgi profili içerir.
İlgi profilini, ilgililik ölçütlerini ve kategori listesini birlikte düzenleyin.
Kategori satırları tam olarak `- "Kategori adı"` biçimindedir. En az bir kategori
gerekir; sabit bir kategori sayısı yoktur. Bu biçimi başka listelerde kullanmayın.
HIGH/MEDIUM/LOW için izin verilen bir kategori, NONE için null beklenir.
Kategori veya profil değiştiğinde eski önbellek sonuçlarını yeniden değerlendirin.

## Ayarlar

`config.py` içindeki temel ayarlar:

| Ayar | Davranış |
|---|---|
| DAYS_BACK | Önceki tamamlanmış günleri tarar; 7 varsayılan, None tüm tarihler |
| SOURCES | Dahil edilecek kaynaklar |
| LIMIT | Kaynak başına kayıt sınırı; None sınırsız |
| FRESH | True: detay/LLM önbelleğini ve indirilen ekleri temizler |
| RECHECK_REFS | Yalnız seçili referansların LLM değerlendirmesini yeniler |
| MAX_CHARS | İhale başına metin bütçesi |
| LLM_MAX_TOKENS | LLM yanıt token bütçesi |
| LLM_TIMEOUT | LLM istek zaman aşımı, saniye |

FRESH kullanmadan önce gerekli verileri yedekleyin. FRESH ve RECHECK_REFS birlikte
kullanılamaz. Seçili yeniden değerlendirme, mevcut tarih/kaynak filtresindeki
kayıtlara uygulanır. İstekler yaklaşık 5,1 saniye aralıkla gönderilir; 429
hatalarında sınırlı yeniden deneme yapılır.

## Çıktılar

- `data/cache/veri_detay.jsonl`: detay kayıtları.
- `data/cache/llm_sonuc.jsonl`: değerlendirmeler ve alıntı kontrolü.
- `data/runs/`: çalışma kimliği içeren Excel raporları.
- `data/logs/`: çalışma logları.
- `data/ekler/` ve `data/browser/`: ek belgeler ve tarayıcı profilleri.

Geçerli bir önbellek kaydı yeniden değerlendirilmez. Eksik detay/LLM sonuçları
rapora yansıyabilir; işlemin bitmesi bütün kayıtların başarıyla değerlendirildiği
anlamına gelmez. Ardışık beş LLM hatasında değerlendirme döngüsü durur.

## İsteğe bağlı Teams bildirimi

TEAMS_WEBHOOK_URL boşsa bildirim atlanır. Doldurulursa, notifications.py içindeki
payload'u kabul eden bir Power Automate/Teams Workflow gerekir: Adaptive Card
`attachments` altında; dosya adı `file_name`, Base64 Excel `file_content` alanındadır.
Akışın dosyayı kaydetmesi ve kartı göndermesi ayrıca yapılandırılır. Bu repo hazır
bir Workflow içermez. HTTP kabulü son teslimin gerçekleştiğini garanti etmez.

## Dosyalar

| Dosya | Görev |
|---|---|
| main.py | Akış, çalışma raporu ve loglama |
| config.py | Ayarlar ve başlangıç doğrulaması |
| sources.py | Liste, detay ve ek toplama |
| attachments.py | Belge metni çıkarma |
| analysis.py | LLM şeması, sınıflandırma ve alıntı kontrolü |
| storage.py | JSONL ve Excel işlemleri |
| notifications.py | İsteğe bağlı Teams bildirimi |

## Doğrulama

```powershell
python -m unittest discover -s tests -v
```

Testler dış servislere bağlanmadan kategori şemasını, yanıt kontrollerini,
örnek değerlendirme–önbellek–Excel akışını ve bildirimsiz çalışmayı doğrular.
Canlı NATO erişimi, gerçek LLM uyumu, Windows Word/Edge ve Teams teslimi için
kendi ortamınızda küçük bir deneme yapın: LIMIT=1 ve FRESH=False ile başlayın.
`.env`, çalışma verileri, tarayıcı profilleri ve loglar Git tarafından dışlanır.
