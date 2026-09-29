export DATABASE_URL ?= postgresql://iris:iris@localhost:5433/iris

.PHONY: up down setup run status test clean

up:
	docker compose up -d --wait

down:
	docker compose down

setup:
	python -m iris_run migrate
	python -m iris_run load-fixtures

run:
	python -m iris_run run

status:
	python -m iris_run status

test:
	python -m pytest -q

clean:
	rm -rf out .pytest_cache
