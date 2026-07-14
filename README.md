# Previsao <> Polymarket BTC 5m Market Maker

Experimental bot and dashboard for quoting the BTC 5-minute market on Previsao using Polymarket prices as the reference.

## English

### What the bot does

The bot watches the BTC 5-minute market on Previsao and Polymarket.

It places one small maker order on Previsao with a configurable discount/margin from the Polymarket price. If someone fills that Previsao order, the bot tries to hedge by buying the opposite side on Polymarket. If hedging is unsafe or cannot be completed, the bot can try to unwind the Previsao position by selling it back on Previsao.

The bot cancels its managed Previsao orders near the end of each round. The default floor is 20 seconds before close.

Use this with very small size first. These are live-money markets. Previsao and Polymarket can settle differently, books can move, APIs can fail, and hedges are not atomic.

### Folder layout

```text
bot/        Python trading bot
dashboard/  Next.js visual dashboard
```

### 1. Run the bot locally in dry-run mode

```bash
git clone https://github.com/ojorgealves/previsao-polymarket-btc5m-bot.git
cd previsao-polymarket-btc5m-bot/bot

cp .env.example .env
# Open .env and fill your Previsao and Polymarket keys.

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

python -m arb_bot --bitcoin-5m --maker-plan --disable-ws --iterations 1 --depth 10
```

Dry-run mode prints what the bot would do. It does not place live orders.

### 2. Run the bot live with small size

Only run this after dry-run works and your `.env` is filled correctly.

```bash
python -m arb_bot \
  --bitcoin-5m \
  --maker-plan \
  --execute-previsao-maker \
  --watch-interval 2 \
  --maker-margin-pct 15 \
  --max-order-usdc 2 \
  --min-seconds-left 20 \
  --depth 50
```

Important knobs:

- `--maker-margin-pct 15`: how far away from Polymarket the Previsao quote should be.
- `--max-order-usdc 2`: maximum size per Previsao quote.
- `--min-seconds-left 20`: stop quoting and cancel orders near close.
- `BOT_MAX_OPEN_ORDERS=1`: keep only one managed Previsao quote open.

### 3. Run the dashboard

```bash
cd ../dashboard
cp .env.example .env.local
# Fill the dashboard password and optional Supabase values.

npm install
npm run dev
```

Open:

```text
http://localhost:3000
```

The dashboard is optional. Supabase is recommended if you want shared config, recent trades, and a remote view.

### Required credentials

Bot:

- Previsao API key and secret.
- Polymarket wallet/private key.
- Polymarket CLOB API key, secret, and passphrase.

Dashboard:

- A strong dashboard password.
- Optional Supabase URL and keys if you want persistent dashboard state.

Never commit `.env` or `.env.local`.

## Português

### O que o bot faz

O bot acompanha o mercado de Bitcoin de 5 minutos na Previsão e na Polymarket.

Ele deixa uma ordem pequena pendurada na Previsão com uma margem/desconto em cima do preço da Polymarket. Se alguém pegar essa ordem na Previsão, o bot tenta se proteger comprando o lado oposto na Polymarket. Se essa proteção não for segura ou não der para completar, o bot pode tentar zerar a posição vendendo de volta na própria Previsão.

Perto do fim da rodada, o bot cancela as ordens abertas que ele controla. O padrão é parar faltando 20 segundos.

Comece com valor bem pequeno. Isso mexe com dinheiro real. Os mercados podem mexer rápido, APIs podem falhar, e a proteção entre Previsão e Polymarket não acontece no mesmo instante.

### Pastas

```text
bot/        bot em Python
dashboard/  painel visual em Next.js
```

### 1. Rodar o bot em modo teste

```bash
git clone https://github.com/ojorgealves/previsao-polymarket-btc5m-bot.git
cd previsao-polymarket-btc5m-bot/bot

cp .env.example .env
# Abra o .env e coloque suas chaves da Previsão e da Polymarket.

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

python -m arb_bot --bitcoin-5m --maker-plan --disable-ws --iterations 1 --depth 10
```

Nesse modo ele só mostra o que faria. Não coloca ordem de verdade.

### 2. Rodar ao vivo com valor pequeno

Só rode depois que o modo teste estiver funcionando.

```bash
python -m arb_bot \
  --bitcoin-5m \
  --maker-plan \
  --execute-previsao-maker \
  --watch-interval 2 \
  --maker-margin-pct 15 \
  --max-order-usdc 2 \
  --min-seconds-left 20 \
  --depth 50
```

Campos importantes:

- `--maker-margin-pct 15`: margem em cima do preço da Polymarket.
- `--max-order-usdc 2`: máximo por ordem na Previsão.
- `--min-seconds-left 20`: parar de colocar ordem perto do fim.
- `BOT_MAX_OPEN_ORDERS=1`: deixar só uma ordem aberta por vez.

### 3. Rodar o painel

```bash
cd ../dashboard
cp .env.example .env.local
# Coloque uma senha forte e, se quiser, os dados do Supabase.

npm install
npm run dev
```

Abra:

```text
http://localhost:3000
```

O painel é opcional. Supabase ajuda se você quiser histórico, config remota e visualização fora da máquina local.

### Chaves necessárias

Bot:

- API key e secret da Previsão.
- Private key/wallet da Polymarket.
- API key, secret e passphrase da Polymarket CLOB.

Painel:

- Uma senha forte.
- Supabase opcional para salvar estado/config.

Nunca coloque `.env` ou `.env.local` no git.

## Deploy notes

For Fly.io, edit `bot/fly.toml`, set your app name, create a volume, set secrets with `fly secrets set`, then deploy:

```bash
cd bot
fly launch --no-deploy
fly volumes create bot_data --size 1
fly secrets set PREVISAO_API_KEY=... PREVISAO_API_SECRET=...
fly secrets set POLYMARKET_PRIVATE_KEY=... POLYMARKET_ADDRESS=... POLYMARKET_API_KEY=... POLYMARKET_API_SECRET=... POLYMARKET_API_PASSPHRASE=...
fly deploy
```

For Vercel, deploy `dashboard/` and set the variables from `dashboard/.env.example`.
