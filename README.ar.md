<div align="center">

<img src="icon.png" width="120" alt="Apple Pie" />

# Apple Pie

**أداة مجانية ومفتوحة المصدر لتجاوز قفل تفعيل iCloud على أجهزة A12+**

[![License](https://img.shields.io/github/license/UserJoo9/ApplePie?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/platform-Windows-0078d4?style=flat-square&logo=windows&logoColor=white)](https://github.com/UserJoo9/ApplePie)
[![iOS](https://img.shields.io/badge/iOS-16%20→%2026.1-black?style=flat-square&logo=apple&logoColor=white)](https://github.com/UserJoo9/ApplePie)
[![Devices](https://img.shields.io/badge/devices-54%20supported-green?style=flat-square)](https://github.com/UserJoo9/ApplePie)

**[🇬🇧 Read in English](README.md)**

</div>

---

كل شيء يعمل محليًا — لا PHP، ولا سيرفر خارجي. الأداة تبني كل الـ payloads وتخدّمها من جهاز الكمبيوتر عبر USB + Wi-Fi المحلي. الواجهة الوحيدة هي المتصفح على `http://localhost:8080`.

---

## كيف تشتغل

iOS بتقرأ MobileGestalt cache وقاعدة بيانات Books/iTunes أثناء التفعيل. الأداة بتحط ملفات مزيّفة على الجهاز وبتعيد تشغيله 3 مرات، عشان iOS يعتقد إن الجهاز اتفعّل قبل كده.

| المرحلة | الملف | النوع | الغرض |
|---------|-------|-------|--------|
| 1 | `fixedfile` | EPUB ZIP | `MobileGestalt.plist` مزيّفة لموديلك |
| 2 | `belliloveu.png` | SQLite | Books DB ببيانات المرحلة الأولى |
| 3 | `apllefuckedhhh.png` | SQLite | Downloads DB بالـ GUID + بيانات المرحلة التانية |

**SystemGroup GUID** — معرّف حاوية Books DB، هو مفتاح الـ bypass وبيتحدد أوتوماتيكيًا من سجلات الجهاز.

---

## المتطلبات

- Windows 10 / 11
- Python 3.11+ (أو استخدم `applepie.exe` الجاهزة)
- iTunes مثبّت (USB drivers بتاعة Apple)
- 3uTools مثبّت *(مستحسن لتوافق الـ drivers)*
- iPhone أو iPad على **شاشة قفل التفعيل**، متوصّل بـ USB وعلى نفس الـ Wi-Fi بتاع الكمبيوتر
- Windows Firewall: السماح بـ TCP **8001** للـ inbound (الأداة تقدر تعمل القاعدة دي أوتوماتيكيًا)

---

## تشغيل سريع (من المصدر)

```
pip install -r requirements.txt
python new_applepie.py
```

افتح **http://localhost:8080**. اتبع الـ checklist على الشاشة — الـ popup مش هيخليك تكمل غير لما كل الشيكات تعدّي.

> لو عندك GUID من قبل: `python new_applepie.py <GUID>`

---

## بناء exe مستقل

```
python -m nuitka --onefile --assume-yes-for-downloads --windows-console-mode=disable ^
  --output-filename=applepie.exe ^
  --include-data-dir=img=img --include-data-dir=data=data ^
  --include-raw-dir=bin=bin --include-data-dir=web=web ^
  --nofollow-import-to=IPython --nofollow-import-to=pytest ^
  new_applepie.py
```

*(الأمر الكامل في `build.txt`.)*
Nuitka هتحمّل MinGW64 أوتوماتيكيًا لو مفيش C compiler.

---

## الأجهزة المدعومة

**54 جهاز** — من iPhone XS لحد iPhone 17، ومن iPad 8 لحد iPad Pro M2 / iPad Air M2 / iPad mini 6.
اضغط أيقونة الـ gear في الواجهة لتصفّح القائمة الكاملة وتفلتر حسب الموديل أو إصدار iOS.

لإضافة جهاز جديد: حط dump صحيح لـ `com.apple.MobileGestalt.plist` في `data/Maker/<ProductType>/` — مش محتاج تغيير في الكود.

---

## حل المشاكل

| المشكلة | الحل |
|---------|------|
| iTunes / 3uTools مش موجودين | ثبّتهم واضغط **Re-check** |
| قاعدة الـ Firewall مش موجودة | اضغط **Apply** (بصلاحيات Administrator) |
| "Device not found" | إعد توصيل USB، اقبل "Trust This Computer" على الموبايل، تحقق من الـ drivers |
| LAN reachability فشلت | في VPN شغّال أو NIC غلط — افتح `ipconfig` وتأكد إن port 8001 مفتوح |
| "Could not get GUID" | امسح بـ Apple Configurator 2 وجرّب تاني، أو ادّي الـ GUID كـ argument |
| "Done" بس لسه مقفول | امسح بـ Apple Configurator 2 وجرّب تاني |

---

## الاختبارات

```
python -m unittest test_webui -v    # 31 اختبار unit/integration
python test_webui_live.py           # 28 اختبار E2E (Edge headless)
python test_webui_pages.py          # 7 screenshots للصفحات
```

---

## هيكل المشروع

```
new_applepie.py          الـ Engine — كل المنطق البرمجي (ApplePieEngine)
webui.py                 Flask bridge (16 API routes)
web/                     واجهة المتصفح (HTML / CSS / JS)
data/
  Maker/<prd>/           مكتبة MobileGestalt (54 جهاز)
  downloads.28.png       SQL dump للمرحلة 3
  BLDatabaseManager.png  SQL dump للمرحلة 2
  badfile.plist          بيتبعت للجهاز
bin/
  libimobiledevice/      ideviceinfo.exe, idevicediagnostics.exe + DLLs
  dependencies/          unifiedlog_iterator.exe
requirements.txt
build.txt                أمر بناء الـ exe
```

---

## إخلاء مسؤولية

الأداة للاستخدام على أجهزتك اللي بتملكها **بشكل قانوني**. تجاوز قفل التفعيل على جهاز مش بتاعك قد يكون مخالفًا للقانون في بلدك.

---

<!-- لو كل الشيكات عدّت بس الجهاز لسه مقفول — افحص إيه اللي الموبايل بيستلمه فعلاً، مش بس إيه اللي السيرفر بيبعته. -->
