# Bot

See the root `README.md` for the full English/Portuguese guide.

Quick dry run:

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m arb_bot --bitcoin-5m --maker-plan --disable-ws --iterations 1 --depth 10
```

Live command, after you understand the risk:

```bash
python -m arb_bot --bitcoin-5m --maker-plan --execute-previsao-maker --watch-interval 2 --maker-margin-pct 15 --max-order-usdc 2 --min-seconds-left 20 --depth 50
```

