# Same Python minor version as the development venv (see requirements-lock.txt).
FROM python:3.12-slim

WORKDIR /app

# rasterio's wheel bundles GDAL but links against the system libexpat, which the
# slim base image no longer ships ("ImportError: libexpat.so.1" otherwise).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first, separately from app code.
# This layer only gets invalidated when the requirements change,
# not on every source edit. The lock pins every package to the version
# tested locally: unpinned, a fresh build picks up whatever is newest
# (e.g. ipyvuetify 3, which drops components the UI uses).
COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements-lock.txt

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
