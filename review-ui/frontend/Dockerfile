FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src

# Fail the build if the Page Sequencing CSS (table + Ground Truth header) did not land.
RUN grep -q 'imaging-sequencing-table' src/styles.css \
 && grep -q 'sequence-truth-head' src/styles.css


RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 3001

CMD ["nginx", "-g", "daemon off;"]
