FROM continuumio/miniconda3

WORKDIR /app

COPY environment.yml requirements.txt ./

RUN conda env create -f environment.yml

SHELL ["conda", "run", "-n", "discord_yt_bot", "/bin/bash", "-c"]

COPY main.py audio_cache.py voice_timing.py music_controls.py youtube.py discord_helpers.py ./

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "discord_yt_bot", "python", "main.py"]
