# Gunicorn reads this file from the working directory on start.
# One process keeps the card cache and the background loop in one place; threads let the
# page, its card refresh and the button calls run at the same time.
workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
