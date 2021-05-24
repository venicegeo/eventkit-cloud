FROM cloudfoundry/cflinuxfs3:0.196.0


RUN groupadd -g 880 eventkit && useradd -u 8800 -g 880 -m eventkit && \
    mkdir -p /var/lib/eventkit/config && chown eventkit:eventkit /var/lib/eventkit

USER eventkit

WORKDIR /var/lib/eventkit

COPY --chown=eventkit ./requirements-dev.txt /var/lib/eventkit/requirements-dev.txt
COPY --chown=eventkit ./environment-dev.yml /var/lib/eventkit/environment-dev.yml
COPY --chown=eventkit ./conda /var/lib/eventkit/conda
COPY --chown=eventkit ./pytest.ini /var/lib/eventkit/pytest.ini
COPY --chown=eventkit ./config/setup.cfg /var/lib/eventkit/config/setup.cfg
COPY --chown=eventkit ./config/pyproject.toml /var/lib/eventkit/config/pyproject.toml

ENV PATH="/home/eventkit/miniconda3/bin:$PATH"
ENV CURL_CA_BUNDLE="/var/lib/eventkit/conda/cacert.pem"
ENV REQUESTS_CA_BUNDLE="/var/lib/eventkit/conda/cacert.pem"
# Install Conda
RUN curl -L https://repo.continuum.io/miniconda/Miniconda3-py37_4.9.2-Linux-x86_64.sh -o miniconda.sh && \
    /bin/bash miniconda.sh -b -p "/home/eventkit/miniconda3" && \
    rm miniconda.sh && \
    \
    # Setup channels, only use channels in environment file or built locally.
    conda config --remove channels defaults && \
    conda config --add channels file://var/lib/eventkit/conda/repo && \
    conda config --set channel_priority strict && \
    \
    # Setup certs (might be needed if self-hosting).
    openssl x509 -outform der -in /var/lib/eventkit/conda/cacert.pem -out /var/lib/eventkit/conda/cacert.crt && \
    conda config --set ssl_verify /var/lib/eventkit/conda/cacert.crt && \
    \
    # Create the environment
    conda env create --force --offline --file environment-dev.yml python=3.9 && \
#    SECRET_KEY=temp_secret_key /home/eventkit/miniconda3/envs/eventkit-cloud/bin/python \
#    /home/eventkit/miniconda3/envs/eventkit-cloud/bin/manage.py collectstatic && \
    \
    # Clean up.
    conda clean --yes --all && \
    rm -rf /var/lib/eventkit/conda && \
    rm -rf /home/eventkit/miniconda3/envs/eventkit-cloud/lib/python3.9/site-packages/eventkit_cloud

COPY --chown=eventkit ./eventkit_cloud /home/eventkit/miniconda3/envs/eventkit-cloud/lib/python3.9/site-packages/eventkit_cloud

ENTRYPOINT ["bash", "/home/eventkit/miniconda3/envs/eventkit-cloud/lib/python3.9/site-packages/scripts/wait-for-postgis.sh"]

CMD ["echo", "This image has no default run command."]
