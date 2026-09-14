# Runbook: DNS Failures After NetworkPolicy Changes

## Symptom

Pods in a namespace suddenly fail to resolve internal or external hostnames, resulting in errors such as:
- `dial tcp: lookup <host> on 10.96.0.10:53: i/o timeout`
- `Could not resolve host: <service>.<namespace>.svc.cluster.local`
- `Temporary failure in name resolution`
- Pod logs show connection timeouts to databases, Redis, or external APIs right after deployment.

This issue typically occurs immediately after applying or updating a `NetworkPolicy` targeting pods in the affected namespace.

## Root Cause

In Kubernetes, once a `NetworkPolicy` selects a pod (via `podSelector`), any unspecified traffic direction is switched to a default-deny mode. If an egress `NetworkPolicy` is applied without explicitly allowing outbound traffic to CoreDNS / `kube-dns` on UDP and TCP port 53, all DNS queries originating from the pod are dropped by the CNI network plugin.

## Check

1. **Test DNS resolution inside an affected pod**:
   ```bash
   kubectl -n <namespace> exec -it <pod-name> -- nslookup kubernetes.default
   ```
   If the lookup hangs and times out, outbound port 53 traffic is likely blocked.

2. **Inspect active NetworkPolicies in the namespace**:
   ```bash
   kubectl -n <namespace> get networkpolicies
   kubectl -n <namespace> describe networkpolicy <policy-name>
   ```
   Check whether `Policy Types` includes `Egress` and whether an egress rule allowing port 53 exists.

3. **Verify CoreDNS status in `kube-system`**:
   ```bash
   kubectl -n kube-system get pods -l k8s-app=kube-dns
   ```
   Ensure CoreDNS pods are running and healthy to rule out a cluster-wide DNS outage.

## Fix

Update the `NetworkPolicy` to include an explicit egress rule allowing UDP and TCP port 53 to CoreDNS in `kube-system`:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: <namespace>
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    # Allow DNS queries to CoreDNS
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
```

Apply the updated NetworkPolicy:

```bash
kubectl apply -f <networkpolicy-file>.yaml
```

Once applied, re-test DNS resolution inside the pod to verify connectivity is restored:

```bash
kubectl -n <namespace> exec -it <pod-name> -- nslookup kubernetes.default
```
