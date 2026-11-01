# کتاب مقدس معماری و نقشه راه جامع اجرایی (Master Engineering Specification v3.0)
## سامانه معاملات خودکار، خرید شرطی و مدیریت هوشمند خروج (TP / SL / METAR)
**پروژه:** `WeatherBet × Polymarket Auto-Trading & Auto-Exit Engine`  
**نسخه سند:** `3.0.0-FINAL (Production Master Blueprint)`  
**وضعیت:** سند مرجع مهندسی و عملیاتی (پوشش ۱۰۰٪ چرخه ورود، پایش، خروج خودکار، اتصال متامسک و انتقال به سرور اختصاصی)

---

## فهرست مطالب
1. [چشم‌انداز، اهداف سیستم و تفاوت با نسخه پایه (Scope & Architecture Vision)](#1)
2. [معماری کلان سیستم و جریان داده (High-Level Architecture & Data Flow)](#2)
3. [ماشین وضعیت کامل پوزیشن: از خرید شرطی تا تسویه (Complete Lifecycle State Machine)](#3)
4. [پروتکل اتصال به کیف‌پول متامسک و حساب پلی‌مارکت (MetaMask × Polymarket CLOB Auth)](#4)
5. [موتور «تعیین اتوماتیک هوشمند» (Smart Auto-Pilot Engine) و فرمول‌های ریاضی](#5)
6. [موتور اجرای ورود: خرید فوری (Marketable) و خرید شرطی (Conditional Limit Buy)](#6)
7. [موتور پایش و خروج خودکار: TP، SL، Trailing Stop و محافظ‌های آب‌وهوایی (METAR)](#7)
8. [هشت قانون آهنین ایمنی و مدیریت خطا (Safety Invariants & Degraded Mode)](#8)
9. [طراحی دقیق پایگاه داده و ساختار فایل‌ها (Database & JSON Schemas)](#9)
10. [مشخصات کامل رابط کاربری: پنجره حرفه‌ای خرید و داشبورد ساده (UI/UX Specification)](#10)
11. [مشخصات APIهای بک‌اند (REST & WebSocket Endpoints)](#11)
12. [پروتکل تست ۳ مرحله‌ای پیش از ورود پول واقعی (PAPER → SHADOW → LIVE)](#12)
13. [راهنمای گام‌به‌گام انتقال به سرور اختصاصی (VPS)، سرویس‌های 24/7 و Private کردن ریپو](#13)
14. [چک‌لیست نهایی پذیرش و تحویل (Final Acceptance Criteria)](#14)

---

<a id="1"></a>
## ۱. چشم‌انداز، اهداف سیستم و تفاوت با نسخه پایه (Scope & Architecture Vision)

### ۱.۱. مسئله اصلی
در بازارهای شرط‌بندی دمای روزانه پلی‌مارکت (Polymarket Weather Markets)، تریدر با ۴ چالش بزرگ روبروست:
1. **نیاز به حضور دائمی پای سیستم:** قیمت باکت‌های دمایی در ساعات انتشار آپدیت مدل‌های هواشناسی (GFS / ECMWF / ICON) یا گزارش‌های METAR فرودگاه‌ها در عرض چند دقیقه جهش یا سقوط می‌کنند.
2. **اسپرد باز و نقدینگی متغیر (Thin Orderbooks):** تفاوت بین `Best Bid` (بهترین خریدار) و `Best Ask` (بهترین فروشنده) گاهی بسیار زیاد است و تصمیم‌گیری بر اساس `Mid-Price` باعث زیان سنگین هنگام فروش می‌شود.
3. **ریسک سوختن ناگهانی باکت با دمای فرودگاه (Intraday METAR Elimination):** برای بازارهای `Highest Temperature`، به محض اینکه دمای لحظه‌ای فرودگاه از سقف یک باکت (مثلاً `72-73°F`) عبور کند و به `74°F` برسد، احتمال برد آن باکت برای سمت `YES` دقیقاً **صفر مطلق** می‌شود.
4. **پیچیدگی تنظیم دستی هر سفارش:** وارد کردن دستی قیمت ورود، حد سود، حد ضرر، اسلیپیج و اندازه تیک برای هر شهر و هر تاریخ زمان‌بر و مستعد خطای انسانی است.

### ۱.۲. راه‌حل جامع این سند (نسخه ۳.۰)
این سامانه، داشبورد ساده (`Simple Dashboard`) را به یک **ترمینال معاملاتی تمام‌اتوماتیک متصل به کیف‌پول متامسک شما** تبدیل می‌کند که بدون نیاز به باز کردن سایت `polymarket.com`:
- **هم ورود به معامله** (به‌صورت خرید فوری `Marketable` یا خرید شرطی در قیمت پایین‌تر `Limit Buy / GTC`) را انجام می‌دهد؛
- **هم مجهز به تیک «تعیین اتوماتیک هوشمند (Smart Auto-Pilot)»** است که با یک کلیک، بهینه‌ترین نقطه ورود، حد سود (بر اساس ارزش منصفانه مدل کالیبره ۱۴ روزه)، حد ضرر و محافظ METAR را تنظیم می‌کند؛
- **هم پس از خرید**، پوزیشن را به‌صورت ۲۴ ساعته روی اوردربوک زنده پلی‌مارکت رصد کرده و در نقاط مشخص‌شده به شکل کاملاً خودکار سیو سود (`Take-Profit`) یا حد ضرر (`Stop-Loss`) را اجرا می‌کند.

---

<a id="2"></a>
## ۲. معماری کلان سیستم و جریان داده (High-Level Architecture & Data Flow)

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                        Simple Dashboard UI (React + Simple HTML)                  │
│  [جدول شهرها و باکت‌ها] ──► [پنجره حرفه‌ای خرید + تیک تعیین اتوماتیک هوشمند]       │
│  [جدول پوزیشن‌های فعال] ──► [ویرایش زنده TP/SL + خروج اضطراری + شبیه‌ساز تیک]     │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │ REST API (/api/locks, /api/polymarket/*)
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                     Application Server & Control Plane (server.ts)                │
│  • اعتبارسنجی قوانین (0 < SL < Entry < TP < 1) و کنترل نسخه (rule_version)         │
│  • محاسبه‌گر Smart Auto-Pilot بر اساس کالیبراسیون هوشمند ۱۴ روزه                    │
│  • هماهنگ‌کننده وضعیت با فایل locked_signals.json و دیتابیس تراکنش‌ها              │
└───────────────────┬───────────────────────────────────────────┬───────────────────┘
                    │                                           │
                    ▼                                           ▼
┌───────────────────────────────────────┐   ┌───────────────────────────────────────┐
│   Weather & METAR Scanner Daemon      │   │   Polymarket Auto-Execution Daemon    │
│   (weatherbot_v3.py / price_monitor)  │   │   (auto_exit_bot.py)                  │
│ • اسکن ۶ ساعته مدل‌ها (GFS/ECMWF/...) │   │ • دریافت زنده Best Bid/Ask از WS      │
│ • رصد ۱۰ دقیقه‌ای METAR فرودگاه‌ها     │   │ • مدیریت سفارش‌های خرید شرطی (GTC)    │
│ • تشخیص Intraday Elimination باکت‌ها  │   │ • موتور TriggerEngine (TP/SL/Trailing)│
└───────────────────┬───────────────────┘   └───────────────────┬───────────────────┘
                    │                                           │
                    └───────────────────┬───────────────────────┘
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│              Polymarket CLOB API & Polygon Blockchain (MetaMask Account)          │
│  • L1 Auth (EIP-712 MetaMask Signer) + L2 Auth (HMAC API Key/Secret/Passphrase)   │
│  • Gnosis Safe Proxy (signature_type=2) / Direct EOA (signature_type=0)           │
│  • Order Placement (FAK / FOK / GTC), Order Cancellation, Fill Reconciliation     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

<a id="3"></a>
## ۳. ماشین وضعیت کامل پوزیشن: از خرید شرطی تا تسویه (Complete Lifecycle State Machine)

هر پوزیشن در طول عمر خود دقیقاً در یکی از ۹ وضعیت زیر قرار دارد:

```
                  ┌──────────────────────┐
                  │      DRAFT / UI      │
                  └──────────┬───────────┘
                             │ ثبت سفارش در پنجره خرید
              ┌──────────────┴──────────────┐
              ▼ (خرید شرطی Limit)           ▼ (خرید فوری Marketable)
    ┌───────────────────┐         ┌───────────────────┐
    │   ENTRY_PENDING   │         │   ENTRY_FILLING   │
    └─────────┬─────────┘         └─────────┬─────────┘
              │                             │
              ├────► [لغو دستی یا METAR] ───┼────► ┌───────────────────┐
              │                             │      │  ENTRY_CANCELLED  │
              ▼ (پر شدن بخشی از خرید)       │      └───────────────────┘
    ┌───────────────────┐                   │
    │   ENTRY_PARTIAL   │◄──────────────────┤
    │ (TP/SL مسلح روی   │                   │
    │  سهم‌های پرشده)    │                   │
    └─────────┬─────────┘                   │
              │ (تکمیل کامل خرید)           │ (پر شدن کامل خرید)
              └──────────────┬──────────────┘
                             ▼
                  ┌──────────────────────┐
                  │       WATCHING       │◄────────────────────┐
                  │ (رصد زنده Best Bid)  │                     │
                  └──────────┬───────────┘                     │
                             │ فعال شدن شرط خروج               │
                             │ (TP / SL / METAR / Trailing)    │
                             ▼                                 │
                  ┌──────────────────────┐                     │
                  │     EXIT_PENDING     │                     │
                  │  (قفل تک‌فرمانی فعال) │                     │
                  └──────────┬───────────┘                     │
                             │                                 │
        ┌────────────────────┼────────────────────┐            │
        ▼ (پر شدن کامل)      ▼ (پر شدن ناقص FAK)  ▼ (رد/منقضی) │
┌───────────────┐   ┌────────────────────┐   ┌─────────────────┴─┐
│    CLOSED     │   │  PARTIALLY_EXITED  │   │  Retry / Re-plan  │
│ (ثبت در PnL)  │   │ (ادامه فروش باقیمانده)│   └───────────────────┘
└───────────────┘   └────────────────────┘
```

### قوانین تغییر وضعیت (State Transition Rules):
1. **`ENTRY_PENDING` $\rightarrow$ `ENTRY_CANCELLED`**:
   - اگر کاربر سفارش **خرید شرطی (`Limit Buy`)** ثبت کرده باشد و قبل از پر شدن آن، دمای واقعی فرودگاه (`observed_intraday_temp`) از سقف باکت عبور کند (`intraday_eliminated == True` برای سمت `YES`)، ربات **بلافاصله** سفارش خرید باز را در پلی‌مارکت Cancel می‌کند تا از خرید باکت سوخته جلوگیری شود.
2. **`ENTRY_PENDING` $\rightarrow$ `ENTRY_PARTIAL` $\rightarrow$ `WATCHING`**:
   - به محض اینکه حتی بخشی از سفارش خرید شرطی پر شود (`filled_shares > 0`)، قوانین خروج خودکار (`TP` و `SL`) روی همان تعداد سهام پرشده مسلح (`Armed`) می‌شوند. وقتی کل حجم سفارش پر شد، وضعیت به `WATCHING` تغییر می‌کند.
3. **`WATCHING` $\rightarrow$ `EXIT_PENDING`**:
   - به محض صدور سیگنال خروج توسط `TriggerEngine`، فیلد `latched_exit_reason` مقداردهی شده و وضعیت به `EXIT_PENDING` می‌رود. در این حالت، هیچ تیک جدیدی نمی‌تواند سفارش خروج دومی ارسال کند (جلوگیری ۱۰۰٪ از Double-Selling).
4. **`EXIT_PENDING` $\rightarrow$ `PARTIALLY_EXITED` یا `CLOSED`**:
   - اگر سفارش فروش کل سهام را نقد کرد، وضعیت به `CLOSED` می‌رود و در کارنامه روزانه ثبت می‌شود.
   - اگر به دلیل کمبود نقدینگی در اوردربوک، سفارش `FAK` فقط بخشی از سهام را فروخت (مثلاً ۷۰ سهم از ۱۰۰ سهم)، مقدار `quantity_shares` به ۳۰ سهم کاهش یافته، وضعیت به `PARTIALLY_EXITED` می‌رود و ربات در سیکل بعدی ۳۰ سهم باقیمانده را مدیریت می‌کند.

---

<a id="4"></a>
## ۴. پروتکل اتصال به کیف‌پول متامسک و حساب پلی‌مارکت (MetaMask × Polymarket CLOB Auth)

### ۴.۱. معماری حساب‌های متصل به MetaMask در پلی‌مارکت
وقتی شما با **MetaMask** وارد سایت پلی‌مارکت می‌شوید، پلی‌مارکت از استاندارد زیر روی شبکه **Polygon (Chain ID: `137`)** استفاده می‌کند:
- **آدرس امضاکننده (`Signer / EOA Address`):** همان آدرس کیف‌پول متامسک شماست که کلید خصوصی (`Private Key`) آن در اختیار شماست.
- **آدرس صندوق/پراکسی (`Funder / Proxy Wallet Address`):** آدرسی که در پروفایل پلی‌مارکت شما نمایش داده می‌شود و موجودی دلاری (`USDC.e`) و سهام شرط‌بندی (`CTF Tokens`) در آن نگهداری می‌شود.
- **انواع امضا (`signature_type`) در کتابخانه رسمی `py-clob-client`:**
  - `signature_type = 0` (**Direct EOA**): زمانی که مستقیماً با خود آدرس متامسک بدون کیف‌پول پراکسی معامله می‌کنید (`funder = signer`).
  - `signature_type = 2` (**Browser Wallet / MetaMask Gnosis Safe Proxy**): حالت استاندارد کاربرانی که متامسک خود را به سایت پلی‌مارکت وصل کرده‌اند (`signer = MetaMask EOA` و `funder = Polymarket Proxy Address`).
  - `signature_type = 1` (**Magic / Email Proxy**): مخصوص کاربرانی که با جیمیل وارد شده‌اند.

### ۴.۲. نحوه احراز هویت دو مرحله‌ای (`L1` و `L2`) در ربات
ربات برای ارتباط با `https://clob.polymarket.com` از جریان زیر استفاده می‌کند:

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

class PolymarketMetaMaskConnector:
    def __init__(self, host="https://clob.polymarket.com", chain_id=137):
        self.private_key = os.environ["POLY_PRIVATE_KEY"]          # کلید خصوصی متامسک (فقط در .env سرور)
        self.funder = os.environ.get("POLY_FUNDER_ADDRESS", "")    # آدرس پراکسی پلی‌مارکت شما
        self.sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))  # 2 برای MetaMask Proxy، 0 برای EOA مستقیم

        self.client = ClobClient(
            host=host,
            key=self.private_key,
            chain_id=chain_id,
            signature_type=self.sig_type,
            funder=self.funder if self.funder else None,
        )
        # تولید یا بازیابی خودکار کلیدهای سطح ۲ (L2 HMAC) برای ارسال فوق‌سریع سفارش
        self.creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
```

### ۴.۳. بررسی‌های ایمنی کیف‌پول قبل از معامله (`Pre-Flight Wallet Checks`)
قبل از اینکه ربات اجازه دهد حالت `LIVE` فعال شود، سرویس `/api/polymarket/account-status` این ۴ مورد را چک و در داشبورد نمایش می‌دهد:
1. **صحت کلید خصوصی و آدرس متامسک (`L1 Signer Verification`)**.
2. **صحت کلیدهای API سطح ۲ (`L2 CLOB Credentials`)**.
3. **موجودی قابل معامله `USDC.e` (`Available Collateral Balance`)** برای ثبت سفارش خرید.
4. **وضعیت مجوز قراردادها (`Allowances`):**
   - مجوز `USDC.e` برای خرید،
   - مجوز توکن‌های شرط (`Conditional Tokens ERC-1155 Approval`) به قرارداد `CTF Exchange` برای اینکه ربات بتواند در لحظهٔ رسیدن به حد سود/ضرر، بدون معطلی سهام شما را بفروشد.

---

<a id="5"></a>
## ۵. موتور «تعیین اتوماتیک هوشمند» (Smart Auto-Pilot Engine) و فرمول‌های ریاضی

برای اینکه هنگام معامله در داشبورد ساده، درگیر تنظیمات پیچیده نشوید، گزینهٔ **«🤖 تعیین اتوماتیک هوشمند (Smart Auto-Pilot)»** به صورت پیش‌فرض تیک خورده است. وقتی این تیک فعال باشد، موتور هوشمند پارامترهای سفارش را با فرمول‌های دقیق زیر محاسبه می‌کند:

### ۵.۱. ورودی‌های موتور هوشمند برای هر باکت
- $P_{\text{ask}}$: بهترین قیمت فروشنده فعلی در اوردربوک (`Best Ask`) برای سمت انتخابی (`YES` یا `NO`).
- $P_{\text{bid}}$: بهترین قیمت خریدار فعلی در اوردربوک (`Best Bid`).
- $P_{\text{model}}$: احتمال کالیبره‌شدهٔ مدل هواشناسی با گیت هوشمند ۱۴ روزه (`smart_model_prob`).
- $P_{\text{belief}}$: باور ترکیبی مدل و بازار (`smart_belief_prob`).
- $\text{EV}$: امید ریاضی سود (`smart_ev`).
- $\Delta_{\text{tick}}$: گام قیمت بازار (`tick_size`، معمولاً `0.01` یا `0.001`).

### ۵.۲. فرمول تعیین اتوماتیک قیمت ورود (`Smart Entry Price`)
1. **در حالت «خرید فوری (Marketable Buy)»:**
   $$P_{\text{entry}} = \text{round\_to\_tick}(P_{\text{ask}}, \Delta_{\text{tick}})$$
   سقف قیمت مجاز خرید برای جلوگیری از اسلیپیج ناگهانی:
   $$P_{\text{max\_buy}} = \min\left(0.99, \text{round\_up}\left(P_{\text{entry}} \times (1 + s_{\text{entry}}), \Delta_{\text{tick}}\right)\right)$$
   که در آن $s_{\text{entry}} = 0.02$ (حداکثر ۲٪ اسلیپیج مجاز ورود).

2. **در حالت «خرید شرطی (Conditional Limit Buy - GTC)»:**
   ربات قیمتی را پیشنهاد می‌دهد که هم پایین‌تر از قیمت فعلی بازار باشد و هم حداقل ۱۵٪ حاشیه سود نسبت به احتمال مدل ایجاد کند:
   $$P_{\text{limit\_buy}} = \text{round\_down}\left(\min\left(P_{\text{ask}} - 0.02, \; P_{\text{model}} \times 0.82\right), \Delta_{\text{tick}}\right)$$
   *(با کف حداقل `0.01` دلار).*

### ۵.۳. فرمول تعیین اتوماتیک حد سود (`Smart Take-Profit`)
هدف حد سود هوشمند این است که **قبل از ریسک تسویهٔ نهایی**، زمانی که قیمت بازار به ارزش منصفانهٔ مدل هواشناسی رسید، سود شما را نقد کند:
$$P_{\text{target\_raw}} = \max\left(P_{\text{model}}, \; P_{\text{belief}}, \; P_{\text{entry}} \times 1.25\right)$$
- اگر باکت دارای سیگنال قوی ($\text{EV} \ge 0.25$) باشد و $P_{\text{model}} > P_{\text{entry}} + 0.04$:
  $$P_{\text{TP}} = \min\left(0.99, \; \max\left(P_{\text{entry}} + 0.03, \; \text{round\_down}(P_{\text{model}}, \Delta_{\text{tick}})\right)\right)$$
- در غیر این صورت (هدف استاندارد ۳۰٪ سود یا حداقل ۴ سنت بالاتر از ورود):
  $$P_{\text{TP}} = \min\left(0.99, \; \max\left(P_{\text{entry}} + 0.03, \; \text{round\_down}(P_{\text{entry}} \times 1.30, \Delta_{\text{tick}})\right)\right)$$

### ۵.۴. فرمول تعیین اتوماتیک حد ضرر (`Smart Stop-Loss`)
در بازارهای آب‌وهوا، باکت‌های ارزان (مثلاً ۱۰ سنت) نوسان درصدی بیشتری نسبت به باکت‌های گران (مثلاً ۶۰ سنت) دارند. بنابراین درصد حد ضرر به شکل پله‌ای و هوشمند تنظیم می‌شود:
- **اگر $P_{\text{entry}} < 0.15$ (زیر ۱۵ سنت):**
  $$P_{\text{SL}} = \max\left(0.01, \; \text{round\_down}(P_{\text{entry}} \times 0.62, \Delta_{\text{tick}})\right) \quad (\approx -38\%)$$
- **اگر $0.15 \le P_{\text{entry}} < 0.35$ (بین ۱۵ تا ۳۵ سنت):**
  $$P_{\text{SL}} = \max\left(0.01, \; \text{round\_down}(P_{\text{entry}} \times 0.72, \Delta_{\text{tick}})\right) \quad (\approx -28\%)$$
- **اگر $P_{\text{entry}} \ge 0.35$ (بالای ۳۵ سنت):**
  $$P_{\text{SL}} = \max\left(0.01, \; \text{round\_down}(P_{\text{entry}} \times 0.78, \Delta_{\text{tick}})\right) \quad (\approx -22\%)$$

### ۵.۵. محافظ‌های فعال در حالت اتوماتیک هوشمند
در حالت `Smart Auto-Pilot`:
- `metar_auto_stop = True` (خروج فوری در صورت عبور دمای METAR از سقف باکت)
- `auto_sell_99c = True` (سیو سود قطعی در `99.5¢`)
- `exit_policy = "MARKETABLE_FAK"`
- `max_slippage_pct = 2.0%`

---

<a id="6"></a>
## ۶. موتور اجرای ورود: خرید فوری (Marketable) و خرید شرطی (Conditional Limit Buy)

### ۶.۱. حالت اول: خرید فوری (`IMMEDIATE_MARKETABLE`)
1. کاربر دکمهٔ ثبت سفارش را می‌زند.
2. ربات تعداد سهام را از روی مبلغ دلاری (`usdc_amount`) و قیمت ورود محاسبه می‌کند:
   $$\text{Quantity (Shares)} = \text{round\_down}\left(\frac{\text{USDC Amount}}{P_{\text{entry}}}, 2\right)$$
3. در حالت `LIVE`، یک سفارش خرید `FAK` (یا `GTC` در قیمت `Best Ask`) به CLOB ارسال می‌شود.
4. به محض تایید پر شدن، وضعیت پوزیشن `WATCHING` شده و پایشگر `TP/SL` فعال می‌گردد.

### ۶.۲. حالت دوم: خرید شرطی (`CONDITIONAL_LIMIT_GTC`)
1. کاربر گزینهٔ **«⏳ خرید شرطی در قیمت مشخص (Limit Buy)»** را انتخاب می‌کند و قیمت دلخواه ورود (مثلاً `18¢` در حالی که قیمت فعلی `24¢` است) و اهداف `TP/SL` (یا تیک اتوماتیک) را تعیین می‌کند.
2. ربات پوزیشن را با وضعیت `ENTRY_PENDING` ثبت می‌کند و در حالت `LIVE` یک سفارش خرید `GTC` روی اوردربوک پلی‌مارکت در قیمت `18¢` می‌گذارد.
3. در هر سیکل پایش (`auto_exit_bot.py` و `price_monitor.py`):
   - **بررسی باطل شدن باکت با METAR:** اگر دمای واقعی فرودگاه از باکت عبور کرد (`intraday_eliminated == True` برای سمت `YES`)، ربات سفارش خرید را کنسل کرده و وضعیت را به `ENTRY_CANCELLED` تغییر می‌دهد.
   - **بررسی پر شدن قیمت (`Best Ask <= limit_buy_price` یا گزارش Fill از CLOB):** به محض اینکه قیمت فروشنده به قیمت شرطی شما رسید (در `SHADOW`/`PAPER`) یا صرافی پر شدن سفارش را گزارش کرد (در `LIVE`)، وضعیت پوزیشن به طور خودکار از `ENTRY_PENDING` به `WATCHING` تغییر کرده و از همان ثانیه، حد سود (`TP`) و حد ضرر (`SL`) روی آن فعال می‌شوند!

---

<a id="7"></a>
## ۷. موتور پایش و خروج خودکار: TP، SL، Trailing Stop و محافظ‌های آب‌وهوایی (METAR)

موتور `TriggerEngine` در هر آپدیت قیمت، شروط خروج را دقیقاً به ترتیب اولویت زیر بررسی می‌کند (اولین شرطی که برقرار شود، پوزیشن را قفل و خارج می‌کند):

| اولویت | کد تریگر (`TriggerReason`) | شرط دقیق فعال‌سازی | توضیح مهندسی |
| :---: | :--- | :--- | :--- |
| **۱** | `MANUAL_FORCE_EXIT` | کلیک کاربر روی دکمهٔ «🚨 خروج اضطراری» | خروج فوری بدون توجه به سایر شروط |
| **۲** | `METAR_ELIMINATED` | `metar_auto_stop == True` و `side == "YES"` و `intraday_eliminated == True` | دمای واقعی فرودگاه از سقف باکت عبور کرده و شانس برد باکت ۰٪ شده است |
| **۳** | `AUTO_SELL_99C` | `auto_sell_99c == True` و `best_bid >= 0.995` | نقد کردن صددرصدی سود در ۹۹.۵ سنت بدون انتظار برای تسویه UMA |
| **۴** | `STOP_LOSS` | `sl_enabled == True` و `best_bid <= sl_price` | فعال شدن حد ضرر بر اساس قیمت واقعی خریدار (`Best Bid`) |
| **۵** | `TAKE_PROFIT` | `tp_enabled == True` و `best_bid >= tp_price` | فعال شدن سیو سود بر اساس قیمت واقعی خریدار (`Best Bid`) |
| **۶** | `TRAILING_STOP` | `trailing_stop_enabled == True` و `peak_bid > entry_price` و `best_bid <= peak_bid * (1 - trailing_offset_pct/100)` | قفل کردن سود کسب‌شده در صورت برگشت روند قیمت از قله (`peak_bid`) |

### فرمول محاسبه قیمت سفارش فروش (`ExecutionPlanner`):
وقتی هر یک از شروط بالا در قیمت $P_{\text{bid}}$ فعال شود، ربات سفارش فروش کورکورانه ارسال نمی‌کند، بلکه کف قیمت مجاز فروش را با توجه به `max_slippage_pct` محاسبه می‌کند:
$$P_{\text{floor}} = \max\left(\Delta_{\text{tick}}, \; \text{round\_down}\left(P_{\text{bid}} \times \left(1 - \frac{\text{max\_slippage\_pct}}{100}\right), \Delta_{\text{tick}}\right)\right)$$
و یک سفارش فروش `SELL` از نوع `FAK` (Fill-And-Kill) با قیمت لیمیت $P_{\text{floor}}$ به اوردربوک می‌فرستد تا تمام خریداران بالای $P_{\text{floor}}$ را جارو کند و هرگز زیر کف مجاز نفروشد.

---

<a id="8"></a>
## ۸. هشت قانون آهنین ایمنی و مدیریت خطا (Safety Invariants & Degraded Mode)

1. **قانون ۱ (ممنوعیت Mid-Price):** هیچ تصمیم خروجی بر اساس میانگین Bid و Ask گرفته نمی‌شود؛ تنها معیار مجاز برای فروش، `Best Bid` واقعی اوردربوک است.
2. **قانون ۲ (نامساوی ریاضی مطلق):** در لحظه ثبت یا ویرایش هر پوزیشن، شرط زیر با نوع داده `Decimal` اجباری است:
   $$0.001 \le P_{\text{SL}} < P_{\text{Entry}} < P_{\text{TP}} \le 0.999$$
3. **قانون ۳ (محافظ داده بیات - `Stale Data Guard`):** اگر عمر آخرین تیک دریافتی از اوردربوک بیشتر از `MAX_PRICE_AGE_MS` (پیش‌فرض ۵۰۰۰ میلی‌ثانیه در حالت WebSocket) باشد، یا `best_bid <= 0` باشد، سیستم وارد وضعیت `DEGRADED` شده و از اجرای استاپ‌لاس روی دیتای فریز شده خودداری می‌کند.
4. **قانون ۴ (قفل تک‌فرمانی - `Exit Latch`):** به محض ورود پوزیشن به `EXIT_PENDING`، کلید یکتای `idempotency_key = sha256(position_id + rule_version + reason)` تولید می‌شود تا ارسال سفارش تکراری غیرممکن باشد.
5. **قانون ۵ (کنترل هم‌زمانی با `rule_version`):** هر بار که کاربر در داشبورد تنظیمات TP یا SL را تغییر می‌دهد، `rule_version` یک واحد افزایش می‌یابد. اگر موتور ربات در حال ارزیابی نسخهٔ قدیمی‌تر باشد، دستور قدیمی لغو و نسخهٔ جدید ملاک قرار می‌گیرد.
6. **قانون ۶ (محافظ باکت سوخته در خرید شرطی):** سفارش‌های `ENTRY_PENDING` در صورت عبور دمای METAR از سقف باکت به صورت خودکار باطل می‌شوند.
7. **قانون ۷ (جداسازی کامل کلید خصوصی):** کلید خصوصی متامسک (`POLY_PRIVATE_KEY`) هرگز در کد، دیتابیس، لاگ‌ها، یا مرورگر قرار نمی‌گیرد و صرفاً از متغیر محیطی سرور خوانده می‌شود.
8. **قانون ۸ (آشتی‌دادن وضعیت در استارت‌آپ - `Startup Reconciliation`):** در بدو روشن شدن سرویس روی سرور، ربات لیست سفارش‌های باز و موجودی واقعی توکن‌ها را از پلی‌مارکت می‌گیرد و با `locked_signals.json` همگام می‌کند.

---

<a id="9"></a>
## ۹. طراحی دقیق پایگاه داده و ساختار فایل‌ها (Database & JSON Schemas)

برای حفظ سازگاری ۱۰۰٪ با ساختار فعلی ریپو و داشبوردها، هر پوزیشن در `data/locked_signals.json` (و جدول متناظر در SQLite سرور) دارای ساختار استاندارد زیر است:

```json
{
  "position_id": "pos_nyc_2026-10-29_517310_1730201000",
  "city": "nyc",
  "city_name": "New York City",
  "date": "2026-10-29",
  "market_id": "517310",
  "token_id": "71321045678901234567890123456789",
  "side": "YES",
  "market_type": "max",
  "bucket_label": "72-73°F",

  "execution_mode": "SHADOW",
  "entry_order_type": "IMMEDIATE_MARKETABLE",
  "smart_autopilot": true,
  "position_state": "WATCHING",
  "status": "open",

  "entry_price": 0.2400,
  "limit_buy_price": null,
  "entry_mid": 0.2400,
  "entry_bid": 0.2300,
  "usdc_amount": 25.00,
  "quantity_shares": 104.16,
  "filled_shares": 104.16,

  "auto_exit_enabled": true,
  "exit_policy": "MARKETABLE_FAK",
  "tick_size": 0.01,
  "max_slippage_pct": 2.0,

  "tp_enabled": true,
  "tp_price": 0.3800,
  "tp_pct": 58.3,
  "sl_enabled": true,
  "sl_price": 0.1700,
  "sl_pct": 29.2,

  "trailing_stop_enabled": false,
  "trailing_offset_pct": 8.0,
  "peak_bid": 0.2400,

  "metar_auto_stop": true,
  "auto_sell_99c": true,

  "rule_version": 1,
  "latched_exit_reason": null,
  "clob_entry_order_id": null,
  "clob_exit_order_id": null,

  "locked_at": "2026-10-29T12:00:00.000Z",
  "last_checked_at": "2026-10-29T12:05:00.000Z",
  "last_price": 0.2600,
  "last_pct": 8.3,
  "closed_at": null,
  "exit_price": null,
  "outcome_pct": null
}
```

### کدهای وضعیت بسته‌شدن (`status` در حالت بسته):
- `closed_auto_tp`: بسته شده توسط سیو سود خودکار (`TAKE_PROFIT` یا `AUTO_SELL_99C`)
- `closed_auto_sl`: بسته شده توسط حد ضرر خودکار (`STOP_LOSS`)
- `closed_auto_metar`: بسته شده توسط محافظ دمای فرودگاه (`METAR_ELIMINATED`)
- `closed_auto_trailing`: بسته شده توسط حد ضرر متحرک (`TRAILING_STOP`)
- `closed_manual`: بسته شده دستی یا با دکمه خروج اضطراری (`MANUAL_FORCE_EXIT`)
- `closed_resolved`: بسته شده پس از تسویه نهایی بازار (`MARKET_RESOLVED`)
- `cancelled_entry`: سفارش خرید شرطی که قبل از پر شدن لغو شده است (`ENTRY_CANCELLED`)

---

<a id="10"></a>
## ۱۰. مشخصات کامل رابط کاربری: پنجره حرفه‌ای خرید و داشبورد ساده (UI/UX Specification)

### ۱۰.۱. پنجره حرفه‌ای ثبت سفارش (`OrderConfigModal`)
هنگام کلیک روی دکمه **«⚡ خرید و تنظیم اتوماتیک»** در هر ردیف از جدول باکت‌ها، مودال حرفه‌ای شامل بخش‌های زیر باز می‌شود:

1. **نوار خلاصه هوشمند باکت (Header Telemetry Bar):**
   - نام شهر، تاریخ، نوع بازار (بیشینه/کمینه)، محدوده دمای باکت.
   - نمایش زنده: `قیمت YES`، `Best Bid`، `Best Ask`، `احتمال مدل هوشمند (Smart Model Prob)`، `باور نهایی` و `EV`.
2. **کارت برجسته «🤖 تیک تعیین اتوماتیک هوشمند (Smart Auto-Pilot)»:**
   - یک چک‌باکس بزرگ و واضح که به صورت پیش‌فرض **فعال (Checked)** است.
   - وقتی فعال است، ربات به طور خودکار بهترین قیمت ورود، `TP` (بر اساس ارزش منصفانه مدل)، `SL` و محافظ‌های METAR را محاسبه کرده و خلاصهٔ آن را در یک باکس شفاف نمایش می‌دهد تا کاربر فقط با انتخاب مبلغ دلاری (`$USDC`) و کلیک روی دکمه تایید، سفارش را ثبت کند.
   - با برداشتن تیک، پنل تنظیمات دستی باز می‌شود.
3. **انتخابگر نوع ورود (Entry Type Selector):**
   - **⚡ خرید فوری در قیمت بازار (`IMMEDIATE_MARKETABLE`)**: خرید آنی و فعال‌سازی بلافاصلهٔ TP/SL.
   - **⏳ خرید شرطی در قیمت پایین‌تر (`CONDITIONAL_LIMIT_GTC`)**: تعیین قیمت شرطی ورود (مثلاً «وقتی قیمت به ۱۸ سنت رسید بخر») + فعال‌سازی خودکار TP/SL به محض پر شدن سفارش.
4. **تنظیم حجم سفارش (`Position Sizing`)**:
   - ورودی مبلغ دلاری (`$USDC`) + دکمه‌های انتخاب سریع (`$5`, `$10`, `$25`, `$50`, `$100`) + محاسبه خودکار تعداد سهام (`Shares`) و نمایش سود خالص دلاری در نقطه TP و زیان دلاری در نقطه SL.
5. **نوار بصری ریسک به ریوارد (`SL ← Entry → TP Visual Bar`)**:
   - نمایش گرافیکی فاصله حد ضرر، نقطه ورود، ارزش منصفانه مدل و حد سود همراه با نسبت `Risk/Reward (R:R)`.
6. **بخش شبیه‌ساز زنده تیک بازار (در حالت ویرایش پوزیشن فعال)**:
   - امکان تست فوری رفتار ربات با وارد کردن یک `Best Bid` فرضی یا شبیه‌سازی عبور دمای METAR.

---

<a id="11"></a>
## ۱۱. مشخصات APIهای بک‌اند (REST Endpoints)

1. **`POST /api/locks` (ثبت سفارش خرید فوری یا شرطی + قوانین Auto-Exit):**
   - اعتبارسنجی کامل `0 < SL < Entry < TP < 1`.
   - پشتیبانی از `entry_order_type` (`IMMEDIATE_MARKETABLE` یا `CONDITIONAL_LIMIT_GTC`) و `smart_autopilot`.
   - در حالت `LIVE`، ارسال سفارش به CLOB از طریق آداپتور پایتون `auto_exit_bot.py`.
2. **`PATCH /api/locks/rules` (ویرایش در لحظه TP / SL / Trailing / قیمت خرید شرطی):**
   - بررسی `source_version == rule_version` برای جلوگیری از Race Condition و افزایش خودکار `rule_version`.
3. **`POST /api/locks/simulate-tick` (تست شبیه‌ساز تیک و بررسی اجرای خودکار):**
   - ارزیابی تیک ورودی روی پوزیشن باز (اعم از فعال‌سازی خرید شرطی `ENTRY_PENDING` یا اجرای خروج `WATCHING` $\rightarrow$ `CLOSED`).
4. **`POST /api/locks/unlock` (حذف قفل، لغو خرید شرطی، یا خروج اضطراری `force_exit`)**.
5. **`GET /api/polymarket/account-status` (بررسی وضعیت کیف‌پول متامسک و کلیدهای CLOB)**:
   - برگرداندن وضعیت تنظیم بودن متغیرهای `POLY_PRIVATE_KEY` و `POLY_FUNDER_ADDRESS`، نوع امضا (`EOA` یا `Gnosis Safe Proxy`) و آماده بودن برای حالت `LIVE`.

---

<a id="12"></a>
## ۱۲. پروتکل تست ۳ مرحله‌ای پیش از ورود پول واقعی (PAPER → SHADOW → LIVE)

برای حفاظت ۱۰۰٪ از سرمایه شما، راه‌اندازی عملیاتی باید دقیقاً طبق این ۳ پله انجام شود:

### مرحله ۱: تست `PAPER` (شبیه‌سازی کامل در داشبورد)
- ثبت یک سفارش خرید فوری و یک سفارش خرید شرطی در حالت `PAPER`.
- باز کردن پنجره «⚙️ تنظیمات / تست TP-SL» و وارد کردن قیمت بالاتر از TP در «شبیه‌ساز تیک بازار» $\leftarrow$ مشاهده بسته شدن خودکار پوزیشن و ثبت آن در «گزارش روزانه» با برچسب `🎯 سیو سود خودکار (TP)`.
- ثبت یک پوزیشن دیگر و تست قیمت پایین‌تر از SL $\leftarrow$ مشاهده ثبت با برچسب `🛑 حد ضرر خودکار (SL)`.
- تست تیک «شبیه‌سازی عبور دمای واقعی فرودگاه (METAR)» $\leftarrow$ مشاهده بسته شدن فوری با برچسب `🌡️ حد ضرر METAR`.

### مرحله ۲: تست `SHADOW` (اجرای زنده روی اوردربوک واقعی پلی‌مارکت بدون ارسال پول)
- قرار دادن سفارش‌ها روی حالت `SHADOW` (پیش‌فرض سیستم).
- در این حالت، ربات قیمت‌های واقعی `Best Bid` و `Best Ask` پلی‌مارکت را رصد می‌کند و وقتی قیمت واقعی بازار به TP یا SL شما برسد، پوزیشن را دقیقاً با قیمت واقعی خریدار در اوردربوک می‌بندد و گزارش می‌دهد، بدون اینکه تراکنشی از کیف‌پول شما کم شود.

### مرحله ۳: تست `LIVE` با حجم میکرو (`$5 USDC`)
- پس از انتقال به سرور اختصاصی و قرار دادن کلید متامسک در فایل `.env`، ابتدا بررسی وضعیت کیف‌پول در داشبورد (`Connected / Ready`).
- ثبت اولین سفارش `LIVE` با مبلغ `$5` روی یک بازار نقدشونده و تایید ثبت سفارش در پلی‌مارکت و اجرای دقیق تغییر `TP/SL`.

---

<a id="13"></a>
## ۱۳. راهنمای گام‌به‌گام انتقال به سرور اختصاصی (VPS)، سرویس‌های 24/7 و Private کردن ریپو

### ۱۳.۱. مشخصات سرور پیشنهادی (VPS)
- **موقعیت جغرافیایی (بسیار مهم):** سرورهای اروپایی خارج از تحریم و خارج از محدودیت پلی‌مارکت (پیشنهاد اول: **هلند - آمستردام**، **آلمان - فرانکفورت**، یا **فنلاند - هلسینکی** از دیتاسنترهایی مثل Hetzner یا OVH). *(توجه: IP سرور نباید آمریکا، فرانسه یا ایران باشد چون CLOB پلی‌مارکت IPهای آمریکا را برای ثبت سفارش بلاک می‌کند).*
- **سخت‌افزار کافی:** ۲ هسته پردازنده (2 vCPU)، ۴ گیگابایت رم (4 GB RAM)، ۲۰ گیگابایت حافظه NVMe SSD، سیستم‌عامل **Ubuntu 22.04 LTS یا 24.04 LTS**.

### ۱۳.۲. معماری اجرایی روی سرور پس از Private کردن ریپو
وقتی کدها را به سرور منتقل و ریپوی گیت‌هاب را **Private** کردید، دیگر نیازی به GitHub Actions نخواهید داشت و ۳ سرویس سبک روی خود سرور شما ۲۴ ساعته کار می‌کنند:

1. **سرویس اول (`weatherbet-web.service`):**
   - اجرای سرور داشبورد و API (`npm start` / `tsx server.ts` روی پورت 3000 با Nginx + SSL یا پورت محافظت‌شده).
2. **سرویس دوم (`weatherbet-autoexit.service`):**
   - اجرای ۲۴ ساعتهٔ دیمون `python3 auto_exit_bot.py --daemon` که هر چند ثانیه/با WebSocket قیمت‌های `Best Bid` پوزیشن‌های باز و سفارش‌های خرید شرطی را چک کرده و در لحظه سفارش خروج یا ورود را اجرا می‌کند.
3. **سرویس سوم (تایمر اسکن مدل‌ها و METAR - `weatherbet-scanner.timer`):**
   - اجرای خودکار اسکن ۶ ساعته مدل‌ها (`weatherbot_v3.py`) و آپدیت ۱۰ دقیقه‌ای قیمت‌ها و گزارش تلگرام (`price_monitor.py`) مستقیماً روی دیسک سرور بدون هیچ تأخیری.

### ۱۳.۳. نمونه فایل `.env` امن روی سرور (با دسترسی `chmod 600 .env`)
```ini
# تنظیمات کیف‌پول متامسک برای اتصال به Polymarket CLOB
POLY_PRIVATE_KEY=0xYOUR_METAMASK_PRIVATE_KEY_HERE
POLY_FUNDER_ADDRESS=0xYOUR_POLYMARKET_PROXY_WALLET_ADDRESS
POLY_SIGNATURE_TYPE=2
POLY_CHAIN_ID=137

# تنظیمات تلگرام (برای ارسال هشدار لحظه‌ای خرید، سیو سود و حد ضرر)
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID
```

---

<a id="14"></a>
## ۱۴. چک‌لیست نهایی اجرای فاز کدنویسی (پس از تأیید شما)

به محض تأیید این سند توسط شما، موارد تکمیلی زیر در کدها نهایی و فعال می‌شوند:
1. [x] هستهٔ محاسباتی `Decimal`، ماشین وضعیت، `TriggerEngine` و `ExecutionPlanner` در `auto_exit_bot.py`.
2. [ ] افزودن **تیک «🤖 تعیین اتوماتیک هوشمند (Smart Auto-Pilot)»** به بالای پنجرهٔ خرید (`OrderConfigModal.tsx`) با محاسبه خودکار نقطه ورود، TP، SL و محافظ‌های METAR.
3. [ ] افزودن قابلیت **«⏳ خرید شرطی (Conditional Limit Buy / GTC)»** در پنجره خرید، بک‌اند (`server.ts`)، مانیتور قیمت (`price_monitor.py`) و هسته پایتون (`auto_exit_bot.py`) به‌همراه لغو خودکار در صورت سوختن باکت با دمای METAR.
4. [ ] تکمیل آداپتور اتصال **MetaMask (`signature_type = 2` Gnosis Proxy و `signature_type = 0` EOA)** و اندپوینت وضعیت کیف‌پول (`/api/polymarket/account-status`) + نمایش وضعیت اتصال کیف‌پول در بالای پنجرهٔ خرید.
5. [ ] قرار دادن دکمهٔ مستقیم دانلود همین سند (`📖 دانلود سند جامع معماری / کتاب مقدس`) در نوار بالای داشبورد.


---

<a id="15"></a>
## ۱۵. جزئیات عمیق فنی بلاکچین پالیگان و قراردادهای هوشمند پلی‌مارکت (Polygon & NegRisk CTF Contracts)

### ۱۵.۱. نکته حیاتی بازارهای دمایی پلی‌مارکت (`neg_risk = True`)
در پلی‌مارکت، بازارهای دمای روزانه شهرها (که شامل ۷ تا ۱۲ باکت دمایی مختلف مثل `70-71°F`, `72-73°F` و ... هستند) از نوع **Negative Risk (`neg_risk: true`)** می‌باشند.
بنابراین هنگام ارسال سفارش واقعی (`LIVE`) از طریق `py-clob-client`، باید به این دو قرارداد هوشمند روی شبکه Polygon (`Chain ID: 137`) توجه شود:
- **قرارداد استاندارد `CTF Exchange`:** `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- **قرارداد چندباکتی `NegRisk CTF Exchange`:** `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- **قرارداد `NegRisk Adapter`:** `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- **توکن دلاری `USDC.e (Bridged USDC)`:** `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

در آداپتور `LiveExecutor` ما، قبل از امضای سفارش، ربات از متد `client.get_neg_risk(token_id)` یا فیلد `neg_risk` بازار استفاده می‌کند و گزینه `PartialCreateOrderOptions(tick_size=..., neg_risk=True)` را به `create_order` پاس می‌دهد تا امضای EIP-712 دقیقاً با قرارداد `NegRisk CTF Exchange` پلی‌مارکت منطبق باشد و هیچ خطای `Invalid Signature` رخ ندهد.

### ۱۵.۲. نحوه استخراج آدرس‌های مورد نیاز از متامسک و پلی‌مارکت
1. **`POLY_PRIVATE_KEY`:** کلید خصوصی حساب متامسک شما که با آن وارد پلی‌مارکت می‌شوید.
2. **`POLY_FUNDER_ADDRESS`:** وقتی در سایت `polymarket.com` با متامسک لاگین هستید، روی آواتار پروفایل خود در بالا سمت راست کلیک کنید؛ آدرسی که زیر نام کاربری یا در بخش Deposit نشان داده می‌شود، آدرس `Proxy Wallet` شماست.
   - اگر این آدرس با آدرس خود متامسک شما متفاوت است $\leftarrow$ `POLY_SIGNATURE_TYPE=2` و `POLY_FUNDER_ADDRESS=<Proxy_Address>`.
   - اگر دقیقاً یکی است $\leftarrow$ `POLY_SIGNATURE_TYPE=0`.

---

<a id="16"></a>
## ۱۶. اسکیمای کامل جداول SQLite برای ممیزی و ثبت تراکنش‌ها (SQLite Audit & Persistence DDL)

علاوه بر همگام‌سازی زنده با `data/locked_signals.json` (جهت نمایش آنی در داشبورد ساده)، روی سرور یک دیتابیس تراکنشی `data/auto_exit_audit.db` با حالت `PRAGMA journal_mode=WAL;` ایجاد می‌شود که شامل جداول زیر است:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ۱. جدول اصلی پوزیشن‌ها و سفارش‌های ورود
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    city TEXT NOT NULL,
    city_name TEXT NOT NULL,
    market_date TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    market_type TEXT NOT NULL DEFAULT 'max',
    bucket_label TEXT NOT NULL,
    neg_risk INTEGER NOT NULL DEFAULT 1,

    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('PAPER', 'SHADOW', 'LIVE')),
    entry_order_type TEXT NOT NULL CHECK (entry_order_type IN ('IMMEDIATE_MARKETABLE', 'CONDITIONAL_LIMIT_GTC')),
    smart_autopilot INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL CHECK (state IN (
        'ENTRY_PENDING', 'ENTRY_PARTIAL', 'WATCHING',
        'EXIT_PENDING', 'PARTIALLY_EXITED', 'CLOSED',
        'ENTRY_CANCELLED', 'ERROR_LOCKED'
    )),

    entry_price TEXT NOT NULL,           -- ذخیره به صورت رشته Decimal دقیق
    limit_buy_price TEXT,                -- قیمت شرطی ورود (در حالت CONDITIONAL_LIMIT_GTC)
    usdc_amount TEXT NOT NULL,
    target_shares TEXT NOT NULL,
    filled_shares TEXT NOT NULL DEFAULT '0',
    remaining_shares TEXT NOT NULL DEFAULT '0',
    tick_size TEXT NOT NULL DEFAULT '0.01',

    latched_exit_reason TEXT,
    active_clob_order_id TEXT,
    exit_price TEXT,
    realized_pnl_usdc TEXT,
    realized_pnl_pct TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

-- ۲. جدول قوانین خروج خودکار با کنترل نسخه (Rule Versioning)
CREATE TABLE IF NOT EXISTS exit_rules (
    position_id TEXT PRIMARY KEY REFERENCES positions(position_id) ON DELETE CASCADE,
    auto_exit_enabled INTEGER NOT NULL DEFAULT 1,
    tp_enabled INTEGER NOT NULL DEFAULT 1,
    tp_price TEXT NOT NULL,
    tp_pct TEXT NOT NULL,
    sl_enabled INTEGER NOT NULL DEFAULT 1,
    sl_price TEXT NOT NULL,
    sl_pct TEXT NOT NULL,
    trailing_stop_enabled INTEGER NOT NULL DEFAULT 0,
    trailing_offset_pct TEXT NOT NULL DEFAULT '8.0',
    peak_bid TEXT,
    metar_auto_stop INTEGER NOT NULL DEFAULT 1,
    auto_sell_99c INTEGER NOT NULL DEFAULT 1,
    exit_policy TEXT NOT NULL DEFAULT 'MARKETABLE_FAK',
    max_slippage_pct TEXT NOT NULL DEFAULT '2.0',
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

-- ۳. جدول تلاش‌های ارسال سفارش به صرافی (با کلید یکتای Idempotency)
CREATE TABLE IF NOT EXISTS order_attempts (
    attempt_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES positions(position_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    intent TEXT NOT NULL CHECK (intent IN ('ENTRY_BUY', 'EXIT_SELL', 'CANCEL_ENTRY')),
    trigger_reason TEXT,
    rule_version INTEGER NOT NULL,
    order_type TEXT NOT NULL CHECK (order_type IN ('FAK', 'FOK', 'GTC')),
    limit_price TEXT NOT NULL,
    observed_best_bid TEXT,
    observed_best_ask TEXT,
    requested_shares TEXT NOT NULL,
    filled_shares TEXT NOT NULL DEFAULT '0',
    avg_fill_price TEXT,
    clob_order_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'SUBMITTED', 'FILLED', 'PARTIAL', 'REJECTED', 'CANCELLED', 'FAILED')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- ۴. جدول لاگ تغییر وضعیت‌ها (Audit Trail)
CREATE TABLE IF NOT EXISTS position_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

---

<a id="17"></a>
## ۱۷. فایل‌های پیکربندی آماده برای سرور اختصاصی لینوکس (`systemd` Services & Timers)

هنگام انتقال به سرور Ubuntu، این ۳ فایل سرویس در مسیر `/etc/systemd/system/` قرار می‌گیرند تا کل سامانه به صورت خودکار و ضدقطع‌شدن (`Restart=always`) اجرا شود:

### ۱۷.۱. سرویس وب و داشبورد (`/etc/systemd/system/weatherbet-web.service`)
```ini
[Unit]
Description=WeatherBet Dashboard & Auto-Trading Control Plane
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/weatherbot
EnvironmentFile=/opt/weatherbot/.env
ExecStart=/usr/bin/npx tsx server.ts
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### ۱۷.۲. سرویس ۲۴ ساعته پایشگر اوردربوک و اجرای خودکار (`/etc/systemd/system/weatherbet-autoexit.service`)
```ini
[Unit]
Description=Polymarket Real-Time Auto-Exit & Limit-Buy Engine
After=network.target weatherbet-web.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/weatherbot
EnvironmentFile=/opt/weatherbot/.env
ExecStart=/opt/weatherbot/venv/bin/python3 auto_exit_bot.py --daemon --interval 3
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

### ۱۷.۳. سرویس و تایمر اسکن خودکار مدل‌ها و METAR (`weatherbet-scanner.service` + `.timer`)
```ini
# /etc/systemd/system/weatherbet-scanner.service
[Unit]
Description=WeatherBet Periodic Model & METAR Scanner
After=network.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/opt/weatherbot
EnvironmentFile=/opt/weatherbot/.env
ExecStart=/bin/bash -c '/opt/weatherbot/venv/bin/python3 price_monitor.py && /opt/weatherbot/venv/bin/python3 dashboard_simple.py'
```

---

<a id="18"></a>
## ۱۸. ماتریس کامل سناریوهای خطا و واکنش دفاعی ربات (Failure Modes & Recovery Matrix)

| ردیف | سناریوی خطا در دنیای واقعی | واکنش خودکار و دفاعی ربات |
| :---: | :--- | :--- |
| **۱** | قطعی اینترنت سرور یا قطع شدن WebSocket پلی‌مارکت | تغییر وضعیت سلامت به `DEGRADED`، توقف اجرای استاپ‌لاس روی دیتای قدیمی، تلاش مجدد اتصال با Exponential Backoff (`1s, 2s, 4s, 8s`) و استعلام REST پس از وصل شدن. |
| **۲** | خالی شدن ناگهانی اوردربوک (مثلاً `Best Bid` از ۳۰ سنت بپرد روی ۲ سنت) | مکانیزم `Slippage Floor` مانع فروش زیر کف مجاز (مثلاً ۲۹.۴ سنت) می‌شود؛ سفارش `FAK` فقط خریداران بالای کف مجاز را پر کرده و مابقی سهام را حراج نمی‌کند. |
| **۳** | پر شدن ناقص سفارش فروش (`Partial Fill`) در باکت‌های کم‌حجم | ثبت دقیق تعداد سهام فروخته‌شده، کاهش `remaining_shares`، انتقال به `PARTIALLY_EXITED` و ادامه رصد برای فروش باقیمانده سهام. |
| **۴** | عبور دمای واقعی فرودگاه (METAR) از سقف باکت در زمانی که سفارش خرید شرطی (`Limit Buy`) هنوز باز است | شناسایی فوری `intraday_eliminated == True`، ارسال دستور `CANCEL` به پلی‌مارکت و تغییر وضعیت به `ENTRY_CANCELLED` قبل از اینکه پول شما وارد باکت بازنده شود. |
| **۵** | کلیک هم‌زمان کاربر روی «ذخیره تغییرات TP/SL» در همان ثانیه‌ای که قیمت به TP قبلی رسیده است | کنترل هم‌زمانی خوش‌بینانه (`Optimistic Concurrency Control`) با فیلد `rule_version`؛ جلوگیری از تداخل و ثبت رویداد در `position_events`. |
| **۶** | ری‌استارت شدن سرور وسط معامله | اجرای خودکار `Startup Reconciliation` در بدو بالا آمدن: خواندن سفارش‌های باز و موجودی واقعی از پلی‌مارکت و تطبیق با `locked_signals.json`. |
| **۷** | فروش دستی یک پوزیشن توسط خود شما در داخل سایت پلی‌مارکت | در سیکل تطبیق موجودی (`Position Balance Sync`)، ربات متوجه صفر شدن موجودی توکن در کیف‌پول متامسک شده و پوزیشن را با وضعیت `EXTERNAL_CLOSED` می‌بندد تا سفارش فروش خطا ندهد. |
