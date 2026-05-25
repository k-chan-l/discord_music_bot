#!/bin/bash

docker rm -f discord-music-bot 2>/dev/null

docker build -t discord-music-bot .

docker run -d \
  --name discord-music-bot \
  --env-file .env \
  discord-music-bot