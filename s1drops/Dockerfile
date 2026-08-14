FROM python:3.11-slim

WORKDIR /app

# Install dependencies first, separately from app code.
# This layer only gets invalidated when requirements.txt changes,
# not on every source edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now bring in the rest of the source and install the package itself.
COPY . .
RUN pip install --no-cache-dir -e .

# Documentation only — doesn't actually publish the port.
# Real publishing happens via `-p` at `docker run` time.
EXPOSE 8765

# --host 0.0.0.0: bind to all interfaces inside the container, not just
#   localhost, or the -p port mapping has nothing to forward to.
# --production: disables the dev file-watcher/auto-restart, which has
#   no business running inside a container.
CMD ["solara", "run", "s1drops.app.main", "--host", "0.0.0.0", "--port", "8765", "--production"]
