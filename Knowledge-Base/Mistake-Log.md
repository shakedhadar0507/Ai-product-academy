# Mistake Log

- Day 4 - UnicodeEncodeError when printing emoji on Windows console (cp1255 codepage). Fix: force UTF-8 stdout. Lesson: Windows terminal encoding can silently break scripts with non-ASCII characters.
