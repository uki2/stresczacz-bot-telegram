# streszczacz – bot Telegrama do głosówek

Bot do grupy na Telegramie: słucha głosówek i odpisuje ich krótkim streszczeniem, luźnym językiem, jak kumpel.

- **Automatycznie:** każda głosówka (też plik audio i „kółko” wideo) od `MIN_DURATION_SECONDS` sekund w górę dostaje streszczenie jako odpowiedź.
- `/skracaj` jako odpowiedź na głosówkę: streszczenie. Dla krótkiej głosówki sam tekst.
- `/tekst` jako odpowiedź na głosówkę: cały tekst.
- `/usun` jako odpowiedź na wiadomość bota: bot ją kasuje (Telegram pozwala do 48 h).
- `/id`: pokazuje ID czatu (do `ALLOWED_CHAT_IDS`).

Przepływ: plik z Telegrama → transkrypcja (Groq Whisper `whisper-large-v3-turbo`) → streszczenie (DeepSeek) → odpowiedź w grupie.

Bot **nie zapisuje historii czatu**. Zwykłe wiadomości tekstowe pomija. W pamięci (RAM) trzyma tylko tekst kilku ostatnich głosówek, żeby `/tekst` nie przepisywał tej samej drugi raz. Znika on przy restarcie.

## 1. Klucze

1. **Telegram:** token od @BotFather. Ustaw `/setprivacy` na **Disable**, żeby bot widział głosówki w grupie, a potem dodaj go do grupy. Jeśli już w niej był, usuń go i dodaj ponownie.
2. **Groq** (transkrypcja): klucz z https://console.groq.com/keys. Darmowy limit wystarcza na kilkanaście głosówek dziennie.
3. **DeepSeek** (streszczenia): klucz z https://platform.deepseek.com/api_keys. Trzeba doładować konto, wystarczy minimalna kwota.

## 2. Konfiguracja

Ustawienia bot czyta ze zmiennych środowiskowych albo z pliku `.env` (wzór w `.env.example`):

| Zmienna | Co to |
|---|---|
| `TELEGRAM_TOKEN` | token od @BotFather |
| `GROQ_API_KEY` | klucz Groq |
| `DEEPSEEK_API_KEY` | klucz DeepSeek |
| `DEEPSEEK_MODEL` | model do streszczeń, domyślnie `deepseek-chat` |
| `ALLOWED_CHAT_IDS` | ID czatów (po przecinku), w których bot działa. Puste = wszędzie. Z obcej grupy bot sam wychodzi |
| `MIN_DURATION_SECONDS` | od ilu sekund głosówka dostaje streszczenie automatycznie, domyślnie 9 |

Pliku `.env` nie wrzucaj do repo ani nikomu nie wysyłaj (jest w `.gitignore`). Na serwerze, do którego mają dostęp inne osoby, lepiej ustawić klucze jako zmienne środowiskowe niż trzymać `.env` w plikach.

## 3. Uruchomienie

Potrzebny Python 3.10+.

```
python -m venv .venv
# Linux/macOS:
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
# Windows:
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python bot.py
```

Bot działa na long pollingu, więc nie potrzebuje otwartego portu ani adresu publicznego. Na hostingu (np. Pterodactyl/Pelican z eggiem „Python generic”) wystarczy wgrać pliki, ustawić plik startowy na `bot.py` i podać zmienne.
