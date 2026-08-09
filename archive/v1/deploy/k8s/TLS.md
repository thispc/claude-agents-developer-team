# Public HTTPS on DOKS, without owning a domain

`https://devteam.152-42-151-175.nip.io`

## How it works

`nip.io` is a public wildcard DNS service: `anything.<ip-with-dashes>.nip.io`
resolves to that IP. That gives a real hostname with no registrar and no cost,
which is all Let's Encrypt needs — HTTP-01 proves control of a *hostname*, and it
does not care who sold it to you.

    browser ──TLS──► DigitalOcean LB ──► ingress-nginx ──► devteam-conductor
                                          (terminates TLS,
                                           cert from Let's Encrypt)

## Three things that cost time, recorded so they do not again

**DOKS only opens firewall ports for NodePort and LoadBalancer services.** It
maintains `k8s-public-access-<cluster>` from those services and reconciles it
every few minutes. Ports added by hand are removed again — which is why running
ingress-nginx on `hostNetwork` port 80 looked like a clever way to avoid paying
for a load balancer and simply never received traffic.

**A LoadBalancer created while its Service is still settling can end up with the
wrong forwarding rules.** The first one forwarded `tcp:80 -> tcp:80` instead of
to the controller's NodePort, and the CCM could not correct it: every retry
returned "Load Balancer can't be updated while it processes previous actions".
The symptom is an empty reply with nothing in any log. Deleting the Service and
recreating it provisions a fresh LB with correct rules.

**Proxy protocol has to match on both sides exactly**, and a mismatch also
presents as an empty reply. It is disabled here on both the LB annotation and the
nginx ConfigMap — the cost is that access logs show the LB's address rather than
the real client, which nothing here depends on.

## Renewal

cert-manager renews about 30 days before expiry with no involvement. The
certificate is stored in the `devteam-tls` Secret; deleting it triggers a
reissue.

Use `letsencrypt-staging` when debugging: the production issuer rate-limits hard,
and `nip.io` is a heavily-used base domain, so repeated failures can lock you out
for a week.

## If you get a real domain

Point an A record at the load balancer and change the two `host:` lines in
`ingress-tls.yaml`. Nothing else changes — same issuer, same ingress, same
certificate flow.
