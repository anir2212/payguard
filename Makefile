.PHONY: case-review-browser case-review-stack-browser identity-check up down build simulate test run stream-up stream-down topics outbox observe-up observe-down

up:
	docker compose --profile stream --profile packaged up -d --build

down:
	docker compose --profile stream --profile packaged down

build:
	docker compose --profile stream --profile packaged build api score console

simulate:
	docker compose --profile stream --profile packaged --profile simulate run --rm simulator

test:
	./gradlew test

run:
	./gradlew run

stream-up:
	docker compose --profile stream up -d

stream-down:
	docker compose --profile stream down

topics:
	docker compose --profile stream exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic payguard.authorization --partitions 6 --replication-factor 1

outbox:
	./gradlew runOutbox

observe-up:
	docker compose --profile observe up -d prometheus grafana

observe-down:
	docker compose --profile observe stop prometheus grafana

case-review-browser:
	cd fraud && .venv/bin/python -m payguard_fraud.case_review_browser

# Need the packaged stack (make up). Each writes compact evidence under bench/.
identity-check:
	cd fraud && .venv/bin/python -m payguard_fraud.stack_identity_check --project payguard

case-review-stack-browser:
	cd fraud && .venv/bin/python -m payguard_fraud.case_review_stack_browser
