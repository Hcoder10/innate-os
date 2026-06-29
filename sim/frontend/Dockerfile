# Single image: carries the node/yarn build tooling AND the Caddy web server.
# The image bakes a cached baseline build; the container rebuilds on start
# (see docker-entrypoint.sh) so web-code changes show up on the next `up`.
FROM node:22-bookworm-slim

# Caddy as a static binary — keeps this a single image, no separate runtime.
COPY --from=caddy:2 /usr/bin/caddy /usr/bin/caddy

WORKDIR /app

# Install dependencies first for layer caching.
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile

# Copy the rest of the source and bake a cached baseline build.
COPY . .
RUN yarn build

# Serving directories: web root (SPA), status (build state/log), config (runtime URLs).
RUN mkdir -p /srv/web /srv/status /srv/config

COPY Caddyfile /etc/caddy/Caddyfile
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
