FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html tsconfig.json vite.config.ts ./
COPY public ./public
COPY src ./src

# Demo gate, inlined into the bundle by Vite. Placeholders unless compose
# passes real values through from review-ui/.env. This is NOT authentication —
# anyone who loads the page can read them.
ARG VITE_LOGIN_USERNAME=demo
ARG VITE_LOGIN_PASSWORD=demo
ENV VITE_LOGIN_USERNAME=$VITE_LOGIN_USERNAME
ENV VITE_LOGIN_PASSWORD=$VITE_LOGIN_PASSWORD

RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 3001

CMD ["nginx", "-g", "daemon off;"]
