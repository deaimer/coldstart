.PHONY: build restart status logs

build:
	go build -o /app/bin/artifact-vault ./cmd/vault

restart: build
	supervisorctl -c /app/config/supervisord.conf restart artifact-api artifact-worker

status:
	supervisorctl -c /app/config/supervisord.conf status

logs:
	tail -n 100 /logs/artifacts/api.log /logs/artifacts/worker.log
