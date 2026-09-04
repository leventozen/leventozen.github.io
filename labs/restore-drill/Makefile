.PHONY: up down destroy seed verify-source backup retrieve drill evidence clean-drill

up:
	./scripts/lab.sh up

down:
	./scripts/lab.sh down

destroy:
	./scripts/lab.sh destroy

seed:
	./scripts/lab.sh seed

verify-source:
	./scripts/lab.sh verify-source

backup:
	./scripts/lab.sh backup

retrieve:
	./scripts/lab.sh retrieve

drill:
	./scripts/restore-drill.sh

evidence:
	@echo "evidence/latest-run.md and evidence/article-snippets.md are written by make drill"

clean-drill:
	@echo "Drill cleanup is included in make drill after verification."

# Project-scoped only. Never runs docker system prune.
