#!/usr/bin/env bash
# One-time deploy of odds-tracker-api (run after the Cloudflare account email is verified).
#   cd /workspace/cf/odds-tracker-api && ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
W="npx --prefix /workspace/cf wrangler"
# 1) first deploy without the self service-binding (the binding target must exist first)
sed '/^# internal fan-out/,$d' wrangler.toml > wrangler.nobind.toml
$W deploy --config wrangler.nobind.toml
# 2) shared key that protects the internal /_part/* routes
KEY=$(head -c 32 /dev/urandom | base64 | tr -dc A-Za-z0-9)
printf '%s' "$KEY" | $W secret put PART_KEY --config wrangler.toml
# 3) final deploy with the SELF binding (fan-out across invocations)
$W deploy --config wrangler.toml
rm -f wrangler.nobind.toml
echo "Done. Test: curl -s https://odds-tracker-api.<your-subdomain>.workers.dev/v1/odds | head -c 300"
