# jarvisbot-gateway

Azure Function gateway pentru JarvisBot.

Expune:
- `POST /api/messages`
- `GET /api/healthz`

Forwardează requesturile botului către:
- `BACKEND_BASE_URL + /api/messages`

## Fișiere
- `function_app.py` - gateway-ul
- `host.json` - config Azure Functions
- `requirements.txt` - dependențe
- `local.settings.example.json` - exemplu config local

## Variabile importante
- `BACKEND_BASE_URL` - URL-ul backend-ului JarvisBot, fără `/api/messages`
