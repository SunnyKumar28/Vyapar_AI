# Deploy Vyapaar AI on Vercel

This configuration deploys the FastAPI application as one Vercel Function. The bundled
`vyapaar.db` seeds each function instance; the application copies it to `/tmp/vyapaar.db`
before opening SQLite, because deployed function files are read-only.

## Required environment variables

Set these in the Vercel project settings. Do not commit them.

```text
SARVAM_API_KEY=<your-key>
VYAPAAR_LLM=sarvam
VYAPAAR_MODEL=sarvam-105b-conversations
VYAPAAR_PII_SECRET=<long-random-secret>
VYAPAAR_APPROVAL_SECRET=<long-random-secret>
```

## Deploy

```bash
npx vercel login
npx vercel link
npx vercel --prod
```

## Demo constraint

Vercel's `/tmp` disk is per function instance and is not durable. This is suitable for
the hackathon demo. A real merchant deployment should replace the SQLite demo store with
managed Postgres and a real WhatsApp Business connector.
