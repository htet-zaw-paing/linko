<div align="center">
  <img src="logo.png" alt="Linko Logo" width="120">
  <h1>Linko Telegram Bot</h1>
  <p>A high-performance Telegram utility to extract media, clip videos, identify songs, and generate AI transcripts.</p>
  
  ![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)
  ![Telegram](https://img.shields.io/badge/Telegram_Bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)
  ![Render](https://img.shields.io/badge/Hosted_on-Render-46E3B7?style=for-the-badge)
</div>

---

<details>
<summary><b>Click here for English User Guide</b></summary>

<br>

### What Linko Can Do
- **Download Audio & Video:** Convert web links into MP3s with cover art, or save watermark-free MP4s (TikTok, IG Reels, YouTube).
- **Smart Clipper:** Precisely trim and download specific timeframes from any video or audio link.
- **Podcast Generator (TTS):** Turn text into ultra-realistic audio podcasts using AI voices.
- **AI Transcribe & Summarize:** Convert voice notes or video files into readable text, and generate instant 5-point summaries for YouTube videos.
- **Music Recognition (Shazam):** Identify songs from voice recordings or forwarded audio clips.
- **Edit ID3 Tags:** Customize song titles, artist names, and album cover art natively in Telegram.
- **Inline Search:** Type `@Linko4UBot` in any chat to search and share music instantly.
- **Extract Audio:** Send any short video file directly to the bot to extract its MP3 track.

### How to Use

**1. Download Media from a Link**
Paste a public link (YouTube, TikTok, Instagram) into the chat. Choose your format:
- <kbd>Video (MP4)</kbd> - Watermark-free video.
- <kbd>Standard (128 kbps)</kbd> - Saves mobile data.
- <kbd>High (320 kbps)</kbd> - Best sound quality.
- <kbd>Voice Note (OGG)</kbd> - Telegram voice memo.

**2. Smart Clip Media**
Type `/clip` followed by the link and the specific timeframe you want to cut.
*Example: `/clip https://youtu.be/xyz 01:15-01:45`*

**3. Generate a Podcast**
Type `/podcast` followed by the text you want to convert into speech (max 1000 characters). 
*Example: `/podcast Welcome to Linko!`*

**4. Edit Tags & Cover Art**
To change the title and artist of an MP3, directly **Reply** to the audio file with `/edit Artist Name - Song Title`. If you attach a square photo to your reply, it will become the new Album Cover.

**5. AI Features & Music Recognition**
- Send a **Voice Note**: Choose to <kbd>Shazam</kbd> the song or <kbd>Transcribe</kbd> the spoken words.
- Send a **Video File**: Choose to <kbd>Extract Audio</kbd> or <kbd>Transcribe Video</kbd>.
- Send a **YouTube Link**: Tap <kbd>Summarize Video (AI)</kbd> to get instant bullet points.

### Command Reference

| Command | Action |
| :--- | :--- |
| `/start` | Restart the bot and view the welcome guide |
| `/search [name]` | Find songs directly inside Telegram |
| `/clip [link] [time]` | Trim and extract specific parts of a video |
| `/podcast [text]` | Convert text to speech |
| `/edit [Artist] - [Title]` | Reply to an audio file to change its tags and cover |
| `/trending` | View the top 10 songs on global music charts |
| `/help` | Open the quick help guide |
| `/quality` | View bitrate comparisons and file sizes |
| `/terms` | View copyright, fair-use, and privacy policies |
| `/about` | View information about Linko |

> **Platform Limits:**
> * Maximum media length: **30 minutes**.
> * Maximum file size: **50 MB** (Large videos are auto-compressed).
> * Processes public links only (no private accounts or paywalls).

</details>

<details>
<summary><b>မြန်မာလမ်းညွှန် ဖတ်ရန် နှိပ်ပါ</b></summary>

<br>

### Linko ဘာတွေလုပ်ပေးနိုင်လဲ
- **သီချင်းနဲ့ ဗီဒီယို ဒေါင်းမယ်:** YouTube ကလင့်ခ်တွေကို Cover ပုံအလှတွေနဲ့ MP3 ပြောင်းပေးသလို၊ TikTok နဲ့ IG က ဗီဒီယိုတွေကို ရေစာ (Watermark) မပါဘဲ ဒေါင်းပေးပါတယ်။
- **အပိုင်းဖြတ်မယ် (Smart Clipper):** ဗီဒီယို ဒါမှမဟုတ် သီချင်းတွေထဲက ကိုယ်လိုချင်တဲ့ အချိန်အပိုင်းအခြားလေးကိုပဲ တိတိကျကျ ဖြတ်ယူပေးပါတယ်။
- **စာသားမှ အသံပြောင်းမယ် (Podcast):** ကိုယ်ရိုက်ထည့်လိုက်တဲ့ စာသားတွေကို AI အသံနဲ့ Podcast အသံဖိုင်အဖြစ် ပြောင်းပေးပါတယ်။
- **AI ဖြင့် အနှစ်ချုပ်မယ် & စာသားပြောင်းမယ်:** Voice Note တွေ၊ Video တွေကို စာသား (Text) အဖြစ်ပြောင်းပေးနိုင်သလို၊ YouTube ဗီဒီယိုတွေကိုလည်း AI သုံးပြီး အဓိကအချက်တွေ အနှစ်ချုပ်ပေးပါတယ်။
- **သီချင်းရှာမယ် (Shazam):** နာမည်မသိတဲ့သီချင်းတွေကို အသံဖမ်းပို့လိုက်ရုံနဲ့ ရှာပေးပါတယ်။
- **Tag နဲ့ Cover ပုံပြင်မယ်:** ဒေါင်းထားတဲ့သီချင်းရဲ့ နာမည်၊ အဆိုတော် နဲ့ Cover ဓာတ်ပုံတွေကို စိတ်ကြိုက် ပြင်ဆင်နိုင်ပါတယ်။
- **Chat တိုင်းမှာရှာမယ်:** မည်သည့် Chat ထဲကနေမဆို `@Linko4UBot` လို့ရိုက်ပြီး သီချင်းတွေ တိုက်ရိုက်ရှာလို့ရပါတယ်။
- **အသံထုတ်ယူမယ် (Extract Audio):** ဖုန်းထဲက ဗီဒီယိုဖိုင်ကို Bot ဆီတိုက်ရိုက်ပို့ပြီး MP3 အသံဖိုင် သီးသန့် ထုတ်ယူနိုင်ပါတယ်။

### ဘယ်လိုသုံးရမလဲ

**၁။ လင့်ခ်ကနေ ဒေါင်းရန်**
YouTube, TikTok သို့မဟုတ် IG က လင့်ခ်တစ်ခုကို ကူးပြီး Linko ဆီပို့လိုက်ပါ။ ပြီးရင် လိုချင်တဲ့ Format ကိုရွေးပါ-
- <kbd>Video (MP4)</kbd> - Watermark မပါတဲ့ ဗီဒီယို။
- <kbd>Standard (128 kbps)</kbd> - ဖုန်းဘေလ်သက်သာတဲ့ ဆိုဒ်အသေး။
- <kbd>High (320 kbps)</kbd> - အသံထွက်အကောင်းဆုံး Quality။
- <kbd>Voice Note (OGG)</kbd> - Telegram Voice Record ပုံစံ။

**၂။ အပိုင်းဖြတ်ယူရန် (Clipper)**
`/clip` လို့ရိုက်ပြီး လင့်ခ် နဲ့ အချိန်ကို ရိုက်ထည့်ပါ။
*ဥပမာ - `/clip https://youtu.be/xyz 01:15-01:45`*

**၃။ စာသားမှ အသံပြောင်းရန် (Podcast)**
`/podcast` လို့ရိုက်ပြီး မိမိပြောင်းလိုသော စာသားကို ဆက်ရိုက်ထည့်ပါ။
*ဥပမာ - `/podcast မင်္ဂလာပါ`*

**၄။ သီချင်းနာမည် နှင့် ဓာတ်ပုံ ပြောင်းရန်**
Bot မှ ပို့ထားသော သီချင်းဖိုင်ကို တိုက်ရိုက် Reply ပြန်ပြီး `/edit Artist Name - Song Title` လို့ရိုက်ထည့်ပါ။ ဓာတ်ပုံတစ်ပုံကိုပါ တွဲပို့ပေးပါက Album Cover အသစ်အဖြစ် ပြောင်းလဲသွားပါမည်။

**၅။ AI အသုံးပြုခြင်း နှင့် သီချင်းရှာခြင်း**
- **Voice Note ဖမ်းပို့ပါက:** <kbd>Shazam</kbd> ဖြင့် သီချင်းရှာနိုင်သလို <kbd>Transcribe</kbd> နှိပ်ပြီး စာသားအဖြစ် ပြောင်းနိုင်ပါတယ်။
- **Video ဖိုင်ပို့ပါက:** <kbd>Extract Audio</kbd> နှိပ်ပြီး အသံထုတ်ယူနိုင်သလို <kbd>Transcribe Video</kbd> ဖြင့် စာသားထုတ်နိုင်ပါတယ်။
- **YouTube လင့်ခ် ပို့ပါက:** <kbd>Summarize Video (AI)</kbd> ကိုနှိပ်ပြီး အနှစ်ချုပ်ဖတ်နိုင်ပါတယ်။

### Commands များ

| Command | လုပ်ဆောင်ချက် |
| :--- | :--- |
| `/start` | Bot ကို စတင်ရန် |
| `/search [name]` | သီချင်းနာမည်နဲ့ ရှာရန် |
| `/clip [link] [time]` | ဗီဒီယို/သီချင်းများကို အချိန်ပိုင်းဖြတ်ယူရန် |
| `/podcast [text]` | စာသားမှ အသံဖိုင်အဖြစ် ပြောင်းရန် |
| `/edit [Artist] - [Title]` | သီချင်းနာမည်၊ အဆိုတော် နဲ့ ဓာတ်ပုံ ပြင်ရန် |
| `/trending` | နာမည်အကြီးဆုံး Top 10 သီချင်းများကို ကြည့်ရန် |
| `/help` | အသုံးပြုနည်း ဖတ်ရန် |
| `/quality` | Audio Quality များအကြောင်း ဖတ်ရန် |
| `/terms` | စည်းမျဉ်းနဲ့ Privacy အကြောင်း ဖတ်ရန် |
| `/about` | Linko အကြောင်း ဖတ်ရန် |

> **ကန့်သတ်ချက်များ -**
> * အရှည်ဆုံး **မိနစ် ၃၀** စာပဲ ဒေါင်းလို့ရပါမယ်။
> * ဖိုင်ဆိုဒ် **50 MB** အထိသာ ရပါမည် (ဆိုဒ်ကြီးပါက Auto-compress လုပ်ပေးပါမည်)။
> * Private ဖြစ်နေတဲ့ အကောင့်တွေ၊ Group တွေက ဗီဒီယိုတွေကို ဒေါင်းလို့မရပါ။

</details>
