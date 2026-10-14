"""Correctness checks for the next-step safety net.

CC-65  A next step that changes the cluster always reaches the engineer marked
       REQUIRES APPROVAL, whichever backend produced it and whether or not the
       model followed the prompt; read-only checks are never marked; marking
       is idempotent.
"""

from __future__ import annotations

import pytest

from app.models import APPROVAL_PREFIX, Hypothesis, mark_if_disruptive

DISRUPTIVE = [
    "kubectl -n payments delete pod billing-api-7f9c",
    "kubectl rollout restart deployment/billing-api",
    "kubectl rollout undo deployment/payments-api",
    "kubectl scale deploy/orders --replicas=0",
    "kubectl apply -f fixed-networkpolicy.yaml",
    "kubectl patch deploy billing-api -p '{...}'",
    "kubectl drain ip-10-0-2-140 --ignore-daemonsets",
    "kubectl exec -it postgres-0 -- psql",
    "helm upgrade m20 oci://ghcr.io/maxuver/charts/minus20",
    "Restart the pod after fixing the Secret",
    "Raise the memory limit to 512Mi",
    "Increase the replica count",
    "Roll back to the previous ReplicaSet",
    "Delete the stale VolumeAttachment csi-7e1c",
    "Update the DB_HOST env var to postgres",
]
READ_ONLY = [
    "kubectl -n payments describe pod billing-api-7f9c",
    "kubectl logs billing-api-7f9c --previous",
    "kubectl get events -n payments --sort-by=.lastTimestamp",
    "kubectl rollout status deployment/billing-api",
    "kubectl rollout history deployment/billing-api",
    "kubectl top pod -n payments",
    "Check whether postgres:5432 answers from the pod (nc -zv postgres 5432)",
    "Compare container_memory_working_set_bytes with the limit",
    "Verify the Secret db-credentials exists in the namespace",
]


@pytest.mark.parametrize("step", DISRUPTIVE)
def test_disruptive_steps_are_marked(step):  # CC-65
    assert mark_if_disruptive(step) == APPROVAL_PREFIX + step


@pytest.mark.parametrize("step", READ_ONLY)
def test_read_only_checks_are_not_marked(step):  # CC-65
    assert mark_if_disruptive(step) == step


def test_marking_holds_for_any_hypothesis_and_is_idempotent():  # CC-65
    h = Hypothesis(root_cause="x", next_steps=["kubectl get pods", "kubectl delete pod p", "REQUIRES APPROVAL: scale to 3"])
    assert h.next_steps == ["kubectl get pods", APPROVAL_PREFIX + "kubectl delete pod p", "REQUIRES APPROVAL: scale to 3"]
    again = Hypothesis(**h.model_dump())
    assert again.next_steps == h.next_steps
