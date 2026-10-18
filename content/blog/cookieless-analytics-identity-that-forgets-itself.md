---
external: false
featured: true
title: "When Privacy Becomes a Distributed Systems Problem: Cookieless Analytics with an Identity That Forgets Itself"
description: "Measuring what a broken checkout costs without storing anything on the visitor's device: a daily salt, server-side sessions, clocks you can't trust, and the grey area it all sits in."
date: 2026-10-18
ogImagePath: "/images/cookieless/og.png"
categories:
  - Distributed Systems
tags:
  - privacy
  - analytics
  - cookieless
  - nats
  - gdpr
references:
  - title: "EDPB Guidelines 2/2023 on the technical scope of Art. 5(3) of the ePrivacy Directive"
    url: "https://www.edpb.europa.eu/system/files/2024-10/edpb_guidelines_202302_technical_scope_art_53_eprivacydirective_v2_en_0.pdf"
  - title: "Plausible: data policy"
    url: "https://plausible.io/data-policy"
  - title: "NATS: Key/value store under the hood (delete vs purge)"
    url: "https://docs.nats.io/learn/key-value/under-the-hood"
  - title: "NATS: Direct Get and read-after-write"
    url: "https://docs.nats.io/learn/jetstream/get-direct"
  - title: "Shopify: Customer Privacy API"
    url: "https://shopify.dev/docs/api/customer-privacy"
---

Most "privacy-friendly analytics" posts stop at "we don't use cookies." That's the easy part. The hard part is the very next question: _if nothing is stored on the visitor's device, how do you know two page views belong to the same visit?_

Here's the kind of day I had to design for. Call the visitor Tuna. Tuna says "no" to the cookie banner at breakfast, switches from Wi-Fi to cellular on the way out, opens two tabs at lunch, scrolls past midnight, and the next day finally accepts and logs in. A completely normal day. Each of those moments shaped the design: some broke my first version, and some are costs I accept on purpose.

![Tuna's day, one person and five visitor ids in two days. 08:10 breakfast on home Wi-Fi, says no to the banner: visitor A, session 1. 08:20 out the door, Wi-Fi to cellular: visitor B, the same person split. 12:30 lunch on office Wi-Fi, two tabs on two servers: visitor C, one session via compare-and-swap. 23:30 on the couch, same IP and browser as breakfast: visitor A again with a new session. 00:05 after midnight, new events get the new salt: visitor D, while a straggler stamped 23:58 stays with A. Day 2, accepts the banner and logs in: visitor E, a fresh id with no bridge back.](/images/cookieless/tunas-day.svg)

Each row comes back later in the post. By the end, the right-hand column should make complete sense.

I'm building a monitoring product for online stores on my own. It tries to answer one question: how much revenue does a store lose when something breaks? Privacy was a hard requirement from day one: **if a visitor says no, we don't store anything on the device, and nothing we compute is built to recognise them tomorrow.** But a "no" doesn't change the store's question: if the checkout broke for that visitor, the store still needs to know what it cost. Squaring those two looked like a privacy decision. It turned out to be a distributed systems problem wearing a privacy hat. Secrets that have to be shared across servers but must never touch disk. Clocks you can't trust. A stateful dependency sitting in the hot path of every request.

This post is the whole story: what I actually needed, the design, the parts I got wrong, and the numbers it makes worse on purpose.

## What I was actually trying to do

It's worth being precise here, because "cookieless analytics" can mean very different things.

Put more concretely: _which problems on a store are costing it sales?_ An error on the checkout page, a slow product page, a broken script. To answer that, I need to compare visits that hit a problem with similar visits that didn't, and see how far each one got in the funnel. That comparison only works if I can tie the error and the order to the same visit, without writing a lasting identifier to the visitor's device. Luckily the question is short-lived: a checkout error and the order it may have cost usually happen minutes apart, so an identity that lives for a day is enough to answer it.

Why not just count things? Because "3% of visits hit an error" and "2% of visits convert" are two separate numbers, and neither says whether the error cost a sale. That takes putting visits that saw the error next to visits that didn't, which means grouping events by visit. So for a visitor who declined tracking, what cookieless mode offers is within-visit attribution of problems to outcomes, not retention, returning-visitor counts or a customer profile. Everything below follows from keeping it that narrow.

So here's what I need from identity:

- Group events into **visits**: this page view and that add-to-cart happened in the same visit.
- Do it **within a single day** on **a single site**.
- Be roughly right **on average**, so funnel ratios hold up.

And here's what I don't need at all:

- Who the person is.
- Whether they came back last week.
- Whether they also shop somewhere else.
- Being exactly right about every single visitor.

That second list is the whole design. Once you write down that you don't need to recognise someone tomorrow, a lot of options become not just unnecessary but actively wrong.

## Why not something like FingerprintJS?

Fingerprinting tools exist to recognise the _same browser over time_, across cleared cookies and incognito windows. European regulators treat it like a cookie: the EDPB's [Guidelines 2/2023](https://www.edpb.europa.eu/system/files/2024-10/edpb_guidelines_202302_technical_scope_art_53_eprivacydirective_v2_en_0.pdf) on Article 5(3) of the ePrivacy Directive place device fingerprinting within its technical scope. Whether an exemption applies is a separate, jurisdiction-specific question, and for analytics I don't rely on one. With consent I don't need fingerprinting, because a plain first-party id is simpler and more accurate. Without consent, persistent fingerprinting is off the table. So the real question is what, if anything, to measure for a visitor who said no.

**What's left is a grey area.** Collecting an error report is the easy part to defend: it keeps the checkout working, which is what the visitor came for. Tying that error to whether the same visit ended in an order is analytics, and that's where it gets grey. Even without cookies the script reads things from the browser, and the same guidelines read that kind of access broadly. They also mention some forms of IP-based tracking. So this doesn't remove the need for a banner, and a "no" on the banner doesn't make the processing automatically lawful.

What I can do is keep that grey area as small as possible: no identity can be reused across days, none crosses sites, and the key behind it is destroyed ten minutes after its day ends. That's data minimisation, not a loophole. So the script has three modes. With consent, it runs normally. Without it, it either runs in cookieless mode, which is everything this post describes, or it stays off and sends nothing. Which mode is available is a deployment and legal-policy decision. On Shopify stores, Shopify's own analytics-processing signal decides, and when it says no, the script stays off. I'm not a lawyer and this isn't legal advice. Whether it's enough for _your_ setup is a conversation for your own lawyer.

## The rules

Before writing a line of code, I wrote down five things the system is never allowed to do. I still go back to this list whenever I'm tempted by a "small" shortcut.

1. **Nothing is persisted on the device.** No cookie, no localStorage, no IndexedDB. Queued events live in page memory and die with the tab. In cookieless mode, the SDK sends events with _no visitor or session identifier_. Not a random id, not a session id. It literally strips `session_id` off the event before it leaves the page. Cookieless payloads carry no customer identifiers either: no user id, no order id, and page URLs lose their query string and fragment in the browser.
2. **No device probing.** No canvas, no font lists, no screen-size-plus-timezone soup, no WebGL. Nothing that tries to recognise the same phone tomorrow.
3. **IP and User-Agent are never stored.** They get used for exactly one thing (you'll see it in a second) and then they're gone. Not in the event, not in the queue, not in the database, not in the logs.
4. **No identity is reusable across days.** Whatever we use to group a visitor's events, it has to become impossible, even for me, to connect Monday's visitor to Tuesday's once the day is over. The day's id can stay on that day's events, but once the key is gone it can't be recomputed from a later request or linked to the next day's id. Ten minutes after midnight, the only key that could do it is purged. If that purge ever fails, an alert fires and a TTL removes the key within two days anyway.
5. **No identity crosses sites.** Someone visiting two different stores that both use the product shows up as two strangers.

There's an obvious question here: _"Wait. You hash IP and User-Agent. Isn't that just fingerprinting with extra steps?"_

Honestly? That's the question I spent the most time on, and I don't think the answer is a clean no. The inputs overlap with what fingerprinting uses, and regulators look at inputs, not intentions. What I can defend is scope and lifetime. A fingerprint exists to recognise a device across days and across sites, ideally in a way the user can't reset. Rules 4 and 5 rule that out: the key you'd need to recompute yesterday's id gets destroyed, and the site is baked into the hash. What's left can only answer one question: "were these two requests, today, on this site, probably from the same browser?" So the claim I'll stand behind isn't "no fingerprinting." It's **no identity that can be recomputed or reused across days, and none that crosses sites.**

I didn't invent this, by the way. Plausible has used roughly this approach for years. What nobody really writes about is what it takes to _run_ it.

## The core trick: a visitor id that forgets itself

![Diagram: the browser sends an event with no identifier; the server hashes site, IP and User-Agent with today's salt into a visitor id, the session store adds a session id, and only the ids reach the queue. IP and User-Agent are used once and never stored.](/images/cookieless/identity-path.svg)

Back to Tuna's breakfast. The first page view leaves the phone with no id on it. So how does the server know the second page view is the same person?

It builds the id itself, from the request, and throws the ingredients away. This is the whole function:

```ts
function cookielessVisitorId(salt, siteId, ip, userAgent) {
  const digest = createHmac('sha256', salt)
    .update(siteId).update('\0')
    .update(ip).update('\0')
    .update(userAgent)
    .digest();
  return `cookieless-${digest.subarray(0, 16).toString('base64url')}`;
}
```

Three ingredients and one secret. Each one earns its place:

- **The site id.** The same person on store A and on store B hashes to completely different ids. So the stored id is site-scoped by construction: the scoping is part of the derivation, not an application-side convention.
- **IP + User-Agent.** The weakest signal that's still useful. Same phone, same network, a few minutes apart? Match. That's all a session needs.
- **The salt.** This is the actual privacy feature. 32 random bytes, a new one every UTC day. Without it, `hash(site, ip, ua)` would be the same forever, and anyone who knows your IP and browser could recompute your id. With it, the id only means something while the salt exists.

So the hash links today's requests on purpose. **What limits it is how the salt is born and how it dies.**

![Timeline: Monday's salt hashes Monday's events, gets a ten-minute grace window after midnight, and is purged at 00:10 on Tuesday. Tuesday's and Wednesday's salts follow the same pattern. If a purge fails, a 48-hour TTL removes the salt anyway.](/images/cookieless/salt-lifecycle.svg)

Here's the life of a salt:

- It lives in a NATS JetStream key-value bucket set to **memory storage only**. JetStream was already the ingest queue, so this is one more bucket, not a new system: three replicas, only the current value. It's never written to disk, and the bucket is left out of snapshots and backups. Three copies means one node restarting at 2 p.m. doesn't quietly hand everyone a new identity for the rest of the day. If all three go down at once, the day's salt is gone for good and everyone starts over as a new visitor. I accept that trade: losing one day's continuity beats keeping a copy on disk.
- It's **purged ten minutes after its day ends.** Yesterday's salt is only needed for the grace window right after midnight (you'll see why at 23:58). At 00:10 UTC the ingest servers purge it from the bucket. A plain KV delete only writes a tombstone and can leave older revisions behind; a purge removes them. If that purge fails, an alert fires and the bucket's 48-hour TTL removes the salt anyway. So the honest bound is ten minutes normally, two days at worst.
- Each ingest server caches today's and yesterday's salt in process memory. Yesterday's leaves the cache at 00:10, together with the bucket copy.

Once Monday's salt is purged, every visitor id from Monday turns into a random-looking string. There's no key left to recompute them with. I wouldn't call that anonymous, because a day's events can still say something about a person. But I can no longer get from an IP and a browser to Monday's id, even if someone asked me nicely.

### Midnight, many servers, one salt

This is the exact moment "privacy feature" turned into "distributed systems problem" for me.

There are several ingest servers. At 00:00 UTC they all need a new salt, and they all need _the same_ salt. Otherwise one visitor's requests get a different id depending on which server they happen to land on, and one person becomes three.

The fix turned out to be one boring primitive: the KV bucket's `create` fails if the key already exists. So every server does the same three steps:

1. `get(today)`. Found it? Use it.
2. Not there? Generate 32 random bytes and try `create(today, bytes)`.
3. Whether that worked or another server beat you to it, `get(today)` again and use whatever's stored.

First server to see a request after midnight picks the day's salt for everyone. No coordinator, no cron job, no rotation script that can silently fail at 3 a.m.

One distinction matters here: only an empty answer means "no salt yet." If the store times out or errors, the server doesn't mint anything. It answers `503` and the browser tries again. Otherwise a slow store at 2 p.m. looks exactly like midnight, and two servers mint two salts.

One catch I only found by reading the fine print: NATS KV doesn't promise read-your-writes when the read hits a follower. Right after another server's `create`, a `get` can still come back empty. So the `get` in step 3 goes to the stream leader, and a server that still sees nothing retries briefly instead of minting a salt of its own.

## Sessions live on the server now

A visitor id tells me "same browser, same day." But what people actually want to see is the _visit_. A breakfast scroll and a lunch-break scroll are two visits, not one. The convention I use: a run of activity with no gap longer than 30 minutes.

With cookies, the browser keeps track of this itself. Without cookies, the phone remembers nothing. So the server has to remember for it, and I keep one tiny record per visitor id in a second memory-only KV bucket:

```json
{ "session_id": "a random uuid", "last_ts": 1759750000000 }
```

When a batch of events shows up, I sort it by event time and walk through it. Gap since `last_ts` over 30 minutes, or no record at all? **New session**, fresh random id. Otherwise? **Same session**, move `last_ts` forward, never back. Write the record back once per batch.

The key has a 2-hour TTL, but that's just garbage collection. Whether a session is still alive is decided by _event timestamps_, never by when the key happens to expire.

Those timestamps come from the browser, so they get one sanity check first. Anything stamped in the future is clamped to the time it arrived. Otherwise a phone with a fast clock could push `last_ts` hours ahead and swallow the next visit into this one.

### The batch that showed up 40 minutes late

A backgrounded tab or a phone in a tunnel can deliver a batch from 40 minutes ago _after_ one from 2 minutes ago.

In my first sketch, the record was "the latest truth" and I just compared timestamps. The late batch looked like a 40-minute gap, started a fake new session, and then overwrote the real one. One delayed batch, two broken sessions.

So now there's a rule: **if an event is more than 30 minutes older than the live session's `last_ts`, it doesn't get to touch the live session at all.** It gets a deterministic id of its own instead:

```ts
staleSessionId = uuidV5(`stale:${visitorId}:${Math.floor(ts / THIRTY_MIN)}`, NAMESPACE)
```

All late events from the same visitor and the same 30-minute slot land in the same session, no matter which server handles them or in what order they arrive. They can't corrupt the live session, and they don't need any shared state. A real visit that straddles a slot boundary gets cut in two, but that's a small, predictable error instead of a random one.

### Lunch: two tabs, two servers, same instant

Lunch break, two tabs open on the same store. Both fire a batch at the same moment, and they land on two different servers. Both read the record, both work out a session, both try to write it back.

The KV store gives me compare-and-swap for free: `update` takes the revision I read and fails if someone else wrote in between. Lose the race? Re-read, redo the work on top of the winner's result. Lose twice? Stop fighting and join whatever session I last read. Two tabs from the same person belong in the same visit anyway. No locks, no leases, just optimistic concurrency on a single key.

### 23:58 on the couch: whose clock decides the day?

Night, still scrolling. An event gets stamped 23:58 UTC, the phone sits on it for a bit, and it reaches the server at 00:01. Which day's salt does it get?

The first version said: the event's own day, from the event's own timestamp. Sounds reasonable until you remember who sets that timestamp. The browser. And browser clocks are wrong _all the time_. A privacy boundary shouldn't depend on whether someone's laptop thinks it's Tuesday.

So now the day is decided by **our** clock. One exception: for the first ten minutes after midnight UTC, an event the browser stamped _before_ midnight still gets yesterday's salt, as long as the stamp falls in the last half hour of the day. So the 23:58 event stays with the rest of the evening instead of becoming a stranger, and a phone whose clock is hours behind gets no exception. Outside that window, an event stamped on an earlier day gets dropped, because placing it would need a salt that's supposed to be gone. This check runs first: the session rules only ever see events whose day is settled. The clock that defines a privacy boundary has to be one you control.

## When the identity machinery breaks

With cookies, if my backend has a bad minute, the browser still knows who it is. With server-side identity, I've put a stateful dependency in the hot path of _every single request_. If the salt store blinks, the server literally can't tell who anyone is.

I got this wrong the first time (more on that below). Since then, every failure goes into one of three buckets, and the rule fits on a sticky note:

![Decision tree: if an event can never be placed, drop and count it. If it can and we still know the visitor, degrade deterministically. If we don't know the visitor yet, retry with a 503. Never drop it quietly and reply 200.](/images/cookieless/failure-buckets.svg)

- **Drop:** the event can't be placed _by design_. Retrying won't help. Drop it, count it.
- **Degrade:** I still know who the visitor is, just not exactly which visit. Pick a coarser, deterministic answer and keep going.
- **Retry:** I can't give a correct answer _right now_, but I could in a second. Return `503`, and the browser holds on to the batch.

The one thing that's not allowed is the secret fourth bucket: losing an event quietly and returning `200`.

So a salt store hiccup at lunchtime is a retry. The server says "not now," the phone waits a second, sends again, and the visitor never notices a thing.

Every drop is counted per reason in a metric, with an alert on it. A sudden spike in "IP missing" usually means some proxy header changed somewhere, and I'd much rather hear that from a graph than from a customer.

### Make every fallback boring and predictable

The degrade bucket hides a subtle trap. When the session store is down, what session id do you hand out? My first instinct was "a random one." Bad idea. It's derived instead:

```ts
fallbackSessionId = uuidV5(`day:${visitorId}:${utcDay(ts)}`, NAMESPACE)
```

Why does this matter? If the session store is flaky, half the servers might be falling back while the other half aren't. A random fallback would give the same visitor a different session on every server, on every retry. A derived one gives them _the same_ session on every server that falls back, without any of them talking to each other. The late-event ids from earlier work exactly the same way.

The principle: **when shared state is gone, degrade to something deterministic, never to something random.** Random fallbacks multiply your error with every retry. Deterministic ones put a ceiling on it.

### "Send the whole batch again" is actually safe

After a `503` (queue full, salt store down), the SDK waits as long as `Retry-After` says and sends the _whole_ batch again. Still no luck? The batch goes back into the SDK's in-memory queue (capped at 100 events) and rides along with the next flush. The phone only lets go of an event once the server actually says yes, or the page is gone.

Wait, the whole batch? Didn't half of it already make it through? Two things make that fine:

1. **Every event carries a random `event_id`, created in the browser at capture time.** The server deduplicates on it. That id identifies the _event_, not the person, so it doesn't break any rule.
2. **The visitor id is idempotent.** Same day + same IP + same User-Agent → same salt → same visitor id. Retried events usually land in the live session too, since they're inside its window. Usually, not always: the session comes from shared state, so a retry after midnight or after a store outage can be placed differently. What actually keeps a retry from counting twice is the `event_id` dedup.

## Anonymous to known: the merge we refuse to do

Next day. Tuna comes back, taps "accept" on the banner this time, and logs in.

This is where every analytics product gets the same feature request: "when someone logs in, connect everything they did before." It's great for funnels. It's also exactly where privacy-first designs quietly die.

### Anonymous visitor → known user: never

A cookieless visitor can't be identified. Full stop. Calling `identify()` in cookieless mode does nothing (the SDK logs a debug warning and drops the call). There's nothing on the phone to attach a user id to, and the server-side id is a daily hash I already promised to forget.

And when the visitor says yes to consent, I **don't** build a bridge. The SDK flushes whatever identity-free events it still had queued, then starts a _brand new_ client identity from zero. The old cookieless id and the new one share nothing.

The tempting shortcut here is "let's just look up which hash had the same IP and User-Agent this morning." The moment you do that, the cookieless id stops being a forgettable grouping key and becomes a tracking id that was just waiting for a name. Rule 4 is dead.

Does this cost me something? Yep. If someone browses anonymously, accepts the banner, then buys, that shows up as two partial journeys. I'm fine with that. A funnel that's a bit pessimistic is a much smaller problem than a consent banner that's lying.

### Consented visitor → known user: forward, never backward

Once a visitor has consented, the browser does have a stable visitor id, and `identify(userId)` attaches the user id to every event from that point on. I store the link as a single row, `(visitor_id → user_id)`, in a small alias table. I don't go back and rewrite anything they did before.

**If a different user id shows up on the same browser, the SDK resets to a brand new visitor and session first.** Think of a shared family laptop, or a support agent logging into customer accounts all day. Without the reset, the second person would inherit the first person's anonymous history. With it, one visitor id can only ever belong to one user.

The rule underneath both cases: **identity can move forward in time, never backward.**

## What you give up

Back to the diagram at the top: one human, three visitors on day one, five ids across two days. And if a colleague on the same office network uses the exact same phone and browser version, they're visitor C too. Here's what that means for the numbers:

**Some people get split.** Wi-Fi to cellular, iCloud Private Relay, VPNs that rotate IPs. Overcounting.

**Some people get merged.** Same office NAT, same mobile carrier, same browser version. Undercounting.

**Midnight UTC cuts everything.** A visit that runs across 00:00 UTC becomes two visitors (apart from that ten-minute grace window for stragglers). If your audience is awake at midnight UTC, you'll notice.

**No returning visitors.** "How many people came back this week?" can't be answered, by design. Neither can multi-day attribution or retention curves. For cookieless traffic, every day starts from zero.

**Some features switch off.** Our session replay relies on a persistent client session, so cookieless mode never records.

The practical takeaway: **treat ratios as directional and absolute counts as approximate.** Splits and merges don't hit every funnel step equally. Someone who adds to cart on Wi-Fi and checks out on cellular becomes one visit that stopped and another that started at checkout, and the conversion rate drops. "We had exactly 4,812 visitors" is not a number to trust either, and it won't match GA. I tell users that up front instead of hoping nobody compares.

And I'm fine with all of it. But go in knowing which numbers will be wrong, and in which direction.

## What the first version got wrong

Everything above is version two. Version one shipped, worked, and had a few mistakes that looked totally fine until I sat down and wrote them out. None of them were about privacy. They were all about what happens to a privacy design on a bad day.

### The restart that made everyone a stranger

Version one kept the salt in memory on a single node. When that node restarted at lunchtime, the next request minted a new salt and everyone on the site became a brand new visitor. No crash, no error, just quietly wrong numbers for the rest of the day. The fix: three replicas, all still in RAM.

### The polite 200 that threw data away

When the salt store couldn't be reached, version one dropped the batch's cookieless events and replied `200`. The SDK, for its part, gave up after three failed attempts even when the server said "try again." Together, a 30-second hiccup could silently erase 30 seconds of everyone's data. That's where the drop / degrade / retry rule came from, and the SDK now holds on to events after a `503` or `429` until the server says yes.

### And the part that wasn't in my code

Rule 3 says IP and User-Agent are never logged. My own code was always fine on that. But a request doesn't reach my code directly. It goes through a CDN and a reverse proxy first, and their access logs are a whole different question. So I audited the whole path and set both up so client IPs don't end up in their access logs.

## If you're building this yourself

The short version of everything above:

1. **Grouping within a day is deliberate; the privacy boundary is the salt's lifecycle.** The hash still links today's requests, so be honest about that. What you can promise is that the link dies with the day: memory-only, purged right after its grace window, never backed up. If you can recompute yesterday's ids, you built a tracker with extra steps.
2. **Memory-only, but never one copy.** Replicate the secret in RAM, or a restart rewrites your numbers for the rest of the day.
3. **Put the site in the hash.** Make the stored id site-scoped by construction, not by convention.
4. **Sort every failure into drop, degrade or retry.** Drop what's impossible by design, degrade deterministically when you still know the visitor, `503` when you can't answer yet. Never a quiet `200`.
5. **Identity moves forward, never backward.** No retroactive stitching from anonymous to known. It's the one shortcut that quietly turns the whole thing into tracking.
6. **Audit the whole path, not just your code.** Your CDN and proxy logs are part of your privacy promise.
7. **Be honest about what's left.** Say which numbers will be wrong and in which direction. Cookieless lowers the legal risk; it doesn't remove the banner, so don't run it where a "no" has to mean nothing is sent.

"Don't store the user" sounds like it should make the system _simpler_. You're storing less, right? In practice it just moves the complexity from the phone to my servers, where it turns into a coordination problem full of secrets, clocks and partial failures. Privacy constraints don't eliminate state. They change where it lives, who coordinates it, and how a failure turns into a wrong number.

Tuna will never know any of this happened. Tuna said no, and through a Wi-Fi switch, a tunnel, two tabs, a salt store hiccup and a midnight, the system kept what came with it: nothing on the phone, nothing that could recognise Tuna the next day, nothing that crossed sites.
