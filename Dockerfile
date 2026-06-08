FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/usr/lib/freecad/lib:/usr/lib/freecad/Mod:/usr/lib/freecad/Ext:/usr/lib/python3/dist-packages
ENV REVERSEPARTS_RUNNING_IN_DOCKER=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        freecad \
    && (apt-get install -y --no-install-recommends python3-freecad || echo "python3-freecad not available, using freecad package Python modules") \
    && python3 -c "import FreeCAD, Part; print('FreeCAD import OK')" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
