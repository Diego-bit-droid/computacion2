.PHONY: run build dev test clean logs

build:
	docker compose build

run: build
	docker compose up

dev:
	# corre el monitor directo en el host (sin Docker), útil mientras se desarrolla
	MONITOR_CONFIG=$(PWD)/config.json python3 src/main.py

test:
	python3 -m pytest tests/ -v

logs:
	tail -f data/monitor.log

clean:
	docker compose down
	rm -f data/dump_*.json data/monitor.log
