"""@skracajbot - streszcza głosówki w grupie.

Bot reaguje tylko na komendy (najlepiej z privacy mode włączonym w @BotFather,
wtedy Telegram w ogóle nie wysyła mu zwykłych wiadomości z grupy):
- /skracaj (odpowiedź na głosówkę): streszczenie, a dla głosówki krótszej niż
  MIN_DURATION_SECONDS sam tekst,
- /tekst (odpowiedź na głosówkę): pełna transkrypcja,
- /usun (odpowiedź na wiadomość bota): bot usuwa tę swoją wiadomość.
Przepływ: pobranie pliku -> transkrypcja (Groq Whisper)
-> streszczenie (DeepSeek) -> odpowiedź w grupie.
"""

import logging
import os
from collections import OrderedDict

import openai
from dotenv import load_dotenv
from groq import AsyncGroq
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

_missing = [k for k in ("TELEGRAM_TOKEN", "GROQ_API_KEY", "DEEPSEEK_API_KEY") if not os.getenv(k)]
if _missing:
    raise SystemExit(
        "Brakuje kluczy w pliku .env: " + ", ".join(_missing) + "\n"
        "Otwórz plik .env w tym folderze, wpisz klucze po znaku = i zapisz plik."
    )

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
# Krótsze głosówki /skracaj tylko przepisuje na tekst, bez streszczania.
MIN_DURATION = int(os.getenv("MIN_DURATION_SECONDS", "9"))
# ID czatów, w których bot działa (oddzielone przecinkami). Puste = działa wszędzie.
ALLOWED_CHAT_IDS = {int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
TG_LIMIT = 4000  # Telegram przyjmuje do 4096 znaków w jednej wiadomości

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
# Logi bez treści, imion i ID czatów: tylko co się stało i ewentualny typ błędu.
log = logging.getLogger("skracajbot")

groq = AsyncGroq()  # czyta GROQ_API_KEY z .env
# DeepSeek ma API zgodne z OpenAI, więc używamy biblioteki openai z adresem DeepSeeka.
deepseek = openai.AsyncOpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

STYLE = """JAK PISAĆ (najważniejsze):
- jak normalny ziomek na czacie, nie jak asystent, nauczyciel ani dziennikarz
- luźno, potocznie, krótko, zwykle małymi literami, bez kropki na końcu
- zero sztywnych zwrotów typu "warto zauważyć", "podsumowując", "w tej wiadomości", "jak prawdziwy..."
- nie tłumacz żartu, nie zaczynaj od "Bo", bez cudzysłowów i myślników, emotki max jedna i tylko jak pasuje
- jak ktoś pisze z błędami albo slangiem, możesz pisać podobnie"""

SYSTEM_PROMPT = """Ktoś z grupy znajomych wrzucił głosówkę, a ty streszczasz ją reszcie, jak kumpel, który przesłuchał za nich.
Dostajesz samą transkrypcję (może mieć błędy rozpoznawania mowy, domyśl się o co chodzi).
- 1-2 krótkie zdania, a jak jest kilka spraw, to parę krótkich punktów
- same konkrety: o co pyta, co proponuje, kiedy, gdzie, kto ma co zrobić
- imię autora bot dopisze sam, więc pisz bez podmiotu (np. "pyta czy ktoś jedzie w sobotę"), nie dodawaj swoich ocen
""" + STYLE

def is_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


# ---------------------------------------------------------------- transkrypcja i streszczenia
# Ostatnie transkrypcje w RAM (file_unique_id -> tekst), żeby /tekst po /skracaj nie płacił drugi raz.
transcripts: OrderedDict[str, str] = OrderedDict()


async def transcribe(audio: bytes, filename: str) -> str:
    result = await groq.audio.transcriptions.create(
        file=(filename, audio),
        model="whisper-large-v3-turbo",
        language="pl",
    )
    return result.text.strip()


async def ask_deepseek(system: str, content: str, temperature: float = 1.0) -> str | None:
    response = await deepseek.chat.completions.create(
        model=DEEPSEEK_MODEL,
        max_tokens=1500,
        temperature=temperature,
        # deepseek-chat to teraz model "myślący": bez tego potrafi zużyć cały limit na myślenie
        # i zwrócić pustą odpowiedź, a do tego jest kilka razy wolniejszy i droższy.
        extra_body={"thinking": {"type": "disabled"}},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
    )
    text = (response.choices[0].message.content or "").strip()
    return text or None


async def summarize(transcript: str) -> str | None:
    return await ask_deepseek(SYSTEM_PROMPT, f"<transkrypcja>\n{transcript}\n</transkrypcja>")


def voice_media(msg):
    """Głosówka, plik audio albo "kółko" wideo -> (media, nazwa pliku) albo None."""
    if msg.voice:
        return msg.voice, "voice.ogg"
    if msg.audio:
        return msg.audio, msg.audio.file_name or "audio.mp3"
    if msg.video_note:
        return msg.video_note, "video_note.mp4"
    return None


def sender_name(msg) -> str:
    return msg.from_user.first_name if msg.from_user else "Ktoś"


async def get_transcript(context: ContextTypes.DEFAULT_TYPE, target) -> str:
    media, filename = voice_media(target)
    key = media.file_unique_id
    if key in transcripts:
        return transcripts[key]
    tg_file = await context.bot.get_file(media.file_id)
    audio = bytes(await tg_file.download_as_bytearray())
    text = await transcribe(audio, filename)
    transcripts[key] = text
    while len(transcripts) > 200:
        transcripts.popitem(last=False)
    return text


async def reply_long(msg, text: str) -> None:
    """Dzieli długi tekst na kilka wiadomości (Telegram ma limit 4096 znaków)."""
    while text:
        if len(text) <= TG_LIMIT:
            part, text = text, ""
        else:
            cut = text.rfind(" ", 0, TG_LIMIT)
            cut = cut if cut > 0 else TG_LIMIT
            part, text = text[:cut], text[cut:].lstrip()
        msg = await msg.reply_text(part)


async def process_voice(context: ContextTypes.DEFAULT_TYPE, target, errors_to) -> None:
    """Transkrypcja + streszczenie głosówki `target`. Błędy idą jako odpowiedź na `errors_to`."""
    media, _ = voice_media(target)
    # Krótkie głosówki tylko przepisujemy (bez DeepSeeka).
    short = (media.duration or 0) < MIN_DURATION

    sender = sender_name(target)
    log.info("/skracaj")
    await context.bot.send_chat_action(target.chat_id, ChatAction.TYPING)

    try:
        transcript = await get_transcript(context, target)
        if not transcript:
            await target.reply_text("nic tu nie słychać 🤷")
            return
        if short:
            await reply_long(target, f"🎙️ {sender}:\n\n{transcript}")
            return
        summary = await summarize(transcript)
    except openai.APIError as e:
        log.error("Błąd API DeepSeek: %s", type(e).__name__)
        await errors_to.reply_text("coś mi się wywaliło, spróbuj za chwilę 😵")
        return
    except Exception as e:
        log.error("Błąd przetwarzania głosówki: %s", type(e).__name__)
        await errors_to.reply_text("nie dałem rady z tą głosówką 😵")
        return

    if summary is None:
        await errors_to.reply_text("tego nie streszczę, sorry")
        return

    minutes, seconds = divmod(media.duration or 0, 60)
    # Odpowiadamy na samą głosówkę, żeby streszczenie było przy niej.
    await target.reply_text(f"🎧 {sender} ({minutes}:{seconds:02d}):\n{summary}")


def voice_target(message):
    """Głosówka, na którą odpowiada komenda, albo komunikat, czego brakuje."""
    target = message.reply_to_message
    if target is None:
        return None, "odpowiedz na głosówkę i wtedy wpisz komendę 👆"
    if voice_media(target) is None:
        return None, "to nie głosówka 🤨"
    return target, None


# ---------------------------------------------------------------- handlery
async def skracaj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skracaj jako odpowiedź na głosówkę -> streszczenie (krótkie: sama transkrypcja)."""
    message = update.effective_message
    if not is_allowed(message.chat_id):
        return
    target, problem = voice_target(message)
    if problem:
        await message.reply_text(problem)
        return
    await process_voice(context, target, message)


async def tekst(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tekst jako odpowiedź na głosówkę -> pełna transkrypcja."""
    message = update.effective_message
    if not is_allowed(message.chat_id):
        return
    target, problem = voice_target(message)
    if problem:
        await message.reply_text(problem)
        return
    sender = sender_name(target)
    log.info("/tekst")
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    try:
        transcript = await get_transcript(context, target)
    except Exception as e:
        log.error("Błąd transkrypcji: %s", type(e).__name__)
        await message.reply_text("nie dałem rady tego przepisać 😵")
        return
    if not transcript:
        await target.reply_text("nic tu nie słychać 🤷")
        return
    await reply_long(target, f"🎙️ {sender}:\n\n{transcript}")


async def usun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/usun jako odpowiedź na wiadomość bota -> bot ją usuwa (tylko swoje wiadomości)."""
    message = update.effective_message
    if not is_allowed(message.chat_id):
        return
    target = message.reply_to_message
    if target is None:
        await message.reply_text("odpowiedz na moją wiadomość i wpisz /usun 👆")
        return
    if not target.from_user or target.from_user.id != context.bot.id:
        await message.reply_text("mogę kasować tylko swoje 🙅")
        return
    try:
        await target.delete()
    except Exception as e:
        log.error("Nie udało się usunąć wiadomości: %s", type(e).__name__)
        await message.reply_text("nie mogę, telegram pozwala kasować tylko do 48h")
        return
    log.info("/usun")
    try:
        await message.delete()  # samą komendę też, jeśli bot jest adminem; jeśli nie, zostaje
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_chat.id):
        return
    await update.effective_message.reply_text(
        "siema 👋 odpowiedz na głosówkę jedną z komend:\n"
        "/skracaj na głosówce: streszczenie\n"
        "/tekst na głosówce: cały tekst\n"
        "/usun na mojej wiadomości: kasuję ją"
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id - pokazuje ID czatu, żeby wpisać je do ALLOWED_CHAT_IDS."""
    chat = update.effective_chat
    await update.effective_message.reply_text(f"id tego czatu: {chat.id}")


async def on_added(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gdy ktoś doda bota do obcej grupy, bot od razu z niej wychodzi."""
    member = update.my_chat_member
    chat = member.chat
    joined = member.new_chat_member.status in ("member", "administrator")
    if joined and chat.type != "private" and not is_allowed(chat.id):
        log.warning("Dodano mnie do obcej grupy - wychodzę")
        await context.bot.leave_chat(chat.id)


async def post_init(app: Application) -> None:
    # Komendy pokazują się w menu Telegrama po wpisaniu "/".
    await app.bot.set_my_commands([
        BotCommand("skracaj", "streszczenie głosówki (odpowiedz na nią)"),
        BotCommand("tekst", "cały tekst głosówki (odpowiedz na nią)"),
        BotCommand("usun", "kasuję swoją wiadomość (odpowiedz na nią)"),
        BotCommand("id", "id tego czatu"),
    ])


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Bez tego python-telegram-bot wypisuje do logów całą wiadomość, przy której był błąd.
    log.error("Błąd: %s", type(context.error).__name__)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("skracaj", skracaj))
    app.add_handler(CommandHandler("tekst", tekst))
    app.add_handler(CommandHandler("usun", usun))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(ChatMemberHandler(on_added, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    if ALLOWED_CHAT_IDS:
        log.info("Działam tylko w dozwolonych czatach (%d)", len(ALLOWED_CHAT_IDS))
    else:
        log.warning("ALLOWED_CHAT_IDS jest puste - bot działa w KAŻDYM czacie. Napisz /id w swojej grupie.")
    log.info("@skracajbot działa. Ctrl+C żeby zatrzymać.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
