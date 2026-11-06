`app.css` is Tailwind built ahead of time (it replaced the 400KB in-browser Tailwind script that blocked every page load).
After adding new Tailwind classes, rebuild from the repo root:

    npx tailwindcss@3.4.17 -i Foundations/static/tailwind.in.css -o Foundations/static/app.css --minify --content "Foundations/app.py,Foundations/agents/*.py"

then bump `?v=` in the `app.css` links in app.py.
