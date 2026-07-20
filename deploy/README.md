# Deploying devteam apps to a cluster

Two clusters matter: a local **kind** cluster for rehearsal, and **DOKS** for
production. The deploy code path is identical; only three things differ, and
each one is handled explicitly rather than discovered in production.

## Rehearse locally first

```bash
brew install kind
kind create cluster --config deploy/kind-cluster.yaml
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/kind/deploy.yaml
kubectl label node devteam-control-plane ingress-ready=true --overwrite
kubectl patch svc ingress-nginx-controller -n ingress-nginx --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/nodePort","value":30080}]'
kubectl create namespace devteam
```

Then deploy from the dashboard (Artifacts → Full deployment) with
`LAUNCHER=k8s`, and reach an app the way a browser with DNS would:

```bash
curl -H "Host: app-11.devteam.local" http://localhost:30080/api/weather?location=eindhoven
```

## What differs on DigitalOcean

**1. The image must be amd64.** DO worker nodes are amd64; an Apple Silicon
machine builds arm64 by default and that image crash-loops with `exec format
error`. `DEPLOY_PLATFORM=linux/amd64` (the default) makes the build cross-compile
whenever it is pushing to a registry. Local kind builds stay native, because the
kind nodes share this host's architecture.

**2. The cluster must be able to pull the image.** kind can be handed an image
directly (`kind load docker-image`); a managed cluster cannot. Set
`DEPLOY_REGISTRY` to a DOCR registry and link it to the cluster so pull
credentials are injected automatically:

```bash
doctl registry create <name>
doctl registry kubernetes-manifest | kubectl apply -f -   # pull secret
doctl kubernetes cluster registry add <cluster>           # link it
```

**3. Ingress costs money — one load balancer, not one per app.**

## The cost decision

A DigitalOcean regional HTTP load balancer is **$12/month per node**
([pricing](https://docs.digitalocean.com/products/networking/load-balancers/details/pricing/)).
A `type: LoadBalancer` Service per app therefore bills *per app*:

| Deployed apps | Per-app LoadBalancer | Shared ingress |
|---|---|---|
| 1  | $12/mo  | $12/mo |
| 5  | $60/mo  | $12/mo |
| 10 | $120/mo | $12/mo |

So `deploy.py` emits `ClusterIP` + an `Ingress` whenever `APPS_DOMAIN` is set
and an ingress controller exists, and only falls back to a per-app
LoadBalancer otherwise (logging that it is doing so). One nginx controller
holds the single billable load balancer; every app is a hostname behind it.

On DOKS, install the cloud provider variant and point wildcard DNS at it:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/do/deploy.yaml
kubectl get svc -n ingress-nginx ingress-nginx-controller   # note EXTERNAL-IP
# then: *.apps.yourdomain.com  A  <EXTERNAL-IP>
```

and set `APPS_DOMAIN=apps.yourdomain.com`, which makes each app resolve at
`app-<project>.apps.yourdomain.com`.

## Sizing for the $140 credit

Control plane is free; you pay for worker Droplets and the one load balancer.

| Item | Monthly | Per day |
|---|---|---|
| 2 × s-2vcpu-2gb worker nodes | $36 | $1.20 |
| 1 × regional HTTP load balancer | $12 | $0.40 |
| DOCR (basic) | $5 | $0.17 |
| **Total** | **$53** | **≈$1.77** |

Roughly **$20 for eleven days** — comfortably inside the credit, leaving most
of it for agent runs. Worker nodes bill per second, so `doctl kubernetes
cluster delete` when you are not testing costs nothing to redo.

Note the cluster runs both the deployed apps *and* (optionally) the worker
Jobs. Two 2GB nodes is enough for a handful of small apps plus a few
concurrent workers; watch `kubectl top nodes` before assuming more.
