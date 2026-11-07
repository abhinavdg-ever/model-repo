FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src

# Fail the build if the Page Sequencing CSS (table + break marks) did not land.
RUN grep -q 'imaging-sequencing-table' src/styles.css \
 && grep -q 'sequence-break-mark' src/styles.css


RUN npm run build

FROM nginx:1.27-alpine

# The backend this frontend talks to. Same compose file: the default. Deployed on its own
# (2026-11-07): set BACKEND_URL, e.g. http://10.0.0.5:4000. nginx fills it in at start.
ENV BACKEND_URL=http://backend:3000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 3001

CMD ["nginx", "-g", "daemon off;"]
