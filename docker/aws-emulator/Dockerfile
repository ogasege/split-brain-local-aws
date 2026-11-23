# Official AWS Emulator container recipe, from the AWS Emulator User Guide
# ("Getting started with Docker"):
# https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-docker.html
# Only change: the package architecture is a build argument
# (x86_64 for Intel/AMD, aarch64 for Apple silicon and other Arm machines).
# There is no version-pinned download URL: LTS/latest moves. Check
# `docker compose run --rm aws-emulator --version` after building.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG EMULATOR_ARCH=x86_64
ADD https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator-${EMULATOR_ARCH}.rpm /tmp/aws-emulator.rpm
RUN rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator
USER 1000
ENV HOME=/home/emulator EMULATOR_BIND_ADDRESS=0.0.0.0
ENTRYPOINT ["aws-emulator"]
