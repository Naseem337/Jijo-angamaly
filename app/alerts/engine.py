"""The alert engine (PLAN.md §5, step 10): tell waiting customers when a matching car arrives.

Who waits: active yard-visitor LEADs (customer said YES) and chat requirements (saved with
consent). Matching is pure code (app.alerts.matcher), so a run costs no AI tokens.

One run:
 1. Expire old leads and requirements.
 2. Read the whole stock. If that fails or returns 0 cars: abort, log an error, change
    nothing (an outage must never look like "every car is new").
 3. Record when each car was first seen. The very first run only records (seeding), so the
    cars already in stock are never alerted.
 4. Outside the send window (09:00–20:00 IST) a real run stops here; the cars stay "new"
    for `new_car_days`, so the next run picks them up.
 5. Per customer (all their wishes together): skip if opted out, paused (a salesperson
    owns the chat), without consent, or after `max_unanswered` alerts with no reply.
    A car counts if it arrived after the wish was made, in the last `new_car_days`, was
    never alerted to them, fits the wish (at most one near match per `max_per_days`) and
    is still available. Best 3 → one template (1/2/3 cars; one car gets its photo).
 6. Each car is claimed in the alert log as 'pending' BEFORE sending, so a crash or a
    second run can never send the same car twice; then marked sent / failed.
 7. Caps: `per_customer_daily` messages per customer and `global_daily_cap` in total.
 --dry-run does every check and writes 'dry_run' rows, but sends nothing.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.alerts.matcher import Fit, car_line, match_car, rank_key, wish_from_requirement
from app.core.client_config import ClientConfig
from app.core.clock import Clock, in_window, ist_today
from app.core.phone import mask_phone
from app.conversation.photos import PhotoSender
from app.inventory import Car, InventoryAdapter, InventoryError
from app.inventory.aliases import ModelAliases
from app.providers.images import ImageError
from app.storage.models import AlertLog, Customer, Lead, LeadRequirement, Message, Requirement, new_id
from app.storage.repository import Repository
from app.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

# Cars recorded by the first (seeding) run get this first-seen time: "always in stock".
SEEDED_AT = datetime(2000, 1, 1, tzinfo=timezone.utc)

ALERT_INTRO = {1: "A car matching your saved requirement is now available:",
               2: "2 cars matching your saved requirement are now available:",
               3: "3 cars matching your saved requirement are now available:"}


@dataclass
class Wish:
    key: str  # "lead:<id>" or "req:<id>", stored as the alert's requirement_id
    phone: str
    want: LeadRequirement
    since: datetime  # only cars that arrived after this
    lead: Lead | None = None


@dataclass
class Pick:
    car: Car
    fit: Fit
    wish: Wish


@dataclass
class AlertReport:
    dry_run: bool
    aborted: str | None = None  # stock unavailable: nothing was changed
    deferred: str | None = None  # e.g. outside the send window: try again later
    seeded: int = 0  # first run: cars recorded, nothing sent
    cars: int = 0
    new_cars: int = 0  # first seen in this run
    recent_cars: int = 0  # arrived within new_car_days
    waiting: int = 0  # customers with an active lead / requirement
    expired: int = 0
    sent: int = 0
    failed: int = 0
    skipped: Counter = field(default_factory=Counter)
    lines: list[str] = field(default_factory=list)  # one per customer acted on, for the CLI


class AlertEngine:
    def __init__(self, config: ClientConfig, repo: Repository, inventory: Callable[[], InventoryAdapter],
                 whatsapp: WhatsAppClient, clock: Clock,
                 photos: Callable[[], PhotoSender | None] = lambda: None) -> None:
        self.config = config
        self.repo = repo
        self._inventory = inventory
        self.whatsapp = whatsapp
        self.clock = clock
        self._photos = photos
        self.rules = config.business_rules.alerts
        self.aliases = ModelAliases(config.business_rules.model_aliases)

    def run(self, *, dry_run: bool = False, pretend_new: frozenset[str] = frozenset()) -> AlertReport:
        """pretend_new: car ids treated as just arrived (for a live test with existing stock)."""
        now, cid = self.clock.now(), self.config.client_id
        report = AlertReport(dry_run=dry_run)
        report.expired = self.repo.expire_leads(cid, now) + self._expire_requirements(now)

        inventory = self._inventory()
        try:
            cars = inventory.list_all_cars()
        except InventoryError as exc:
            cars, report.aborted = [], f"stock unavailable: {exc}"
        if not cars:
            report.aborted = report.aborted or "stock returned 0 cars"
            log.error("ALERT RUN ABORTED for %s: %s. Nothing marked seen.", cid, report.aborted)
            return report
        report.cars = len(cars)

        seen = self.repo.get_seen_cars(cid)
        if not seen:
            self.repo.mark_cars_seen(cid, [c.id for c in cars], SEEDED_AT)
            report.seeded = len(cars)
            log.info("First alert run for %s: recorded %d cars already in stock, sent nothing", cid, len(cars))
            if not pretend_new:
                return report
            seen = {c.id: SEEDED_AT for c in cars}
        new_ids = [c.id for c in cars if c.id not in seen]
        self.repo.mark_cars_seen(cid, new_ids, now)
        seen.update({car_id: now for car_id in [*new_ids, *pretend_new]})
        report.new_cars = len(new_ids)

        window = self.rules.send_window
        if not in_window(now, window.start, window.end):
            report.deferred = f"outside the send window ({window.start}–{window.end} IST)"
            if not dry_run:
                return report

        since = now - timedelta(days=self.rules.new_car_days)
        recent = [c for c in cars if c.is_available and seen.get(c.id, SEEDED_AT) >= since]
        report.recent_cars = len(recent)
        wishes: dict[str, list[Wish]] = defaultdict(list)
        for wish in self._wishes(now):
            wishes[wish.phone].append(wish)
        report.waiting = len(wishes)
        if recent:
            for phone, customer_wishes in wishes.items():
                self._alert_customer(phone, customer_wishes, recent, seen, inventory, now, report)
        return report

    # ------------------------------------------------------------------ who is waiting

    def _wishes(self, now: datetime) -> list[Wish]:
        cid, wishes = self.config.client_id, []
        for status in ("active", "notified"):  # notified = alerted before; still waiting
            for lead in self.repo.list_leads(cid, status=status, limit=10_000):
                if lead.expires_at > now and not lead.requirement.is_empty():
                    wishes.append(Wish(f"lead:{lead.id}", lead.phone, lead.requirement, lead.created_at, lead))
        for req in self.repo.list_requirements(cid, "active"):
            want = wish_from_requirement(req)
            if req.expires_at > now and not want.is_empty():
                wishes.append(Wish(f"req:{req.id}", req.phone, want, req.created_at))
        return wishes

    def _expire_requirements(self, now: datetime) -> int:
        expired = 0
        for req in self.repo.list_requirements(self.config.client_id, "active"):
            if req.expires_at <= now:
                req.status, req.updated_at = "expired", now
                self.repo.save_requirement(req)
                expired += 1
        return expired

    # ------------------------------------------------------------------ one customer

    def _alert_customer(self, phone: str, wishes: list[Wish], recent: list[Car], seen: dict[str, datetime],
                        inventory: InventoryAdapter, now: datetime, report: AlertReport) -> None:
        cid, who = self.config.client_id, mask_phone(phone)
        customer = self.repo.get_customer(cid, phone)

        def skip(reason: str) -> None:
            report.skipped[reason] += 1
            report.lines.append(f"{who}: skipped ({reason})")

        if customer is None or customer.opted_out:
            return skip("opted out")
        if customer.bot_paused:
            return skip("chat is with a salesperson")
        wishes = [w for w in wishes if w.lead or customer.consent]  # an active lead carries its own YES
        if not wishes:
            return skip("no consent")

        picks = self._picks(phone, wishes, recent, seen, inventory, now)
        if not picks:
            return
        if self._unanswered(phone, customer) >= self.rules.max_unanswered:
            return skip(f"{self.rules.max_unanswered} alerts with no reply")
        day = ist_today(self.clock).isoformat()
        if not report.dry_run:
            if self.repo.get_counter(cid, f"alert_msgs:{phone}", day) >= self.rules.per_customer_daily:
                return skip("already alerted today")
            if not self.repo.increment_counter(cid, "alert_sends", day, cap=self.rules.global_daily_cap):
                report.deferred = f"daily cap of {self.rules.global_daily_cap} alerts reached"
                return skip("daily cap reached")
            self.repo.increment_counter(cid, f"alert_msgs:{phone}", day)

        batch = new_id()
        claimed = [p for p in picks
                   if self.repo.claim_alert(AlertLog(cid, phone, p.car.id, p.wish.key, p.fit, now, batch_id=batch))]
        if not claimed:
            return skip("already alerted (another run)")
        ids = [p.car.id for p in claimed]
        lines = [car_line(p.car, p.fit, p.wish.want) for p in claimed]
        if report.dry_run:
            for car_id in ids:
                self.repo.mark_alert(cid, phone, car_id, "dry_run", at=now)
            report.lines.append(f"{who}: would send {len(ids)} car(s): {', '.join(ids)}")
            return
        self._send(customer, claimed, lines, batch, now, report)

    def _picks(self, phone: str, wishes: list[Wish], recent: list[Car], seen: dict[str, datetime],
               inventory: InventoryAdapter, now: datetime) -> list[Pick]:
        """Best cars for this customer (exact first), at most one near match, still available."""
        already = self.repo.alerted_car_ids(self.config.client_id, phone)
        near_allowed = not self._near_recently(phone, now)
        best: dict[str, Pick] = {}
        for wish in wishes:
            for car in recent:
                if car.id in already or seen.get(car.id, SEEDED_AT) <= wish.since:
                    continue
                fit = match_car(car, wish.want, self.aliases, self.rules.near_match)
                if fit is None or (fit == "near" and not near_allowed):
                    continue
                if car.id not in best or (fit == "match" and best[car.id].fit == "near"):
                    best[car.id] = Pick(car, fit, wish)
        chosen: list[Pick] = []
        for pick in sorted(best.values(), key=lambda p: rank_key(p.car, p.fit, p.wish.want)):
            if pick.fit == "near" and any(c.fit == "near" for c in chosen):
                continue
            try:
                fresh = inventory.get_car(pick.car.id)  # re-check right before sending
            except InventoryError:
                fresh = None
            if fresh is not None and fresh.is_available:
                chosen.append(Pick(fresh, pick.fit, pick.wish))
            if len(chosen) == self.rules.max_cars_per_message:
                break
        return chosen

    def _unanswered(self, phone: str, customer: Customer) -> int:
        """Alert messages sent since the customer last wrote to us."""
        alerts = self.repo.list_alerts(self.config.client_id, phone=phone, since=customer.last_inbound_at)
        return len({a.batch_id for a in alerts if a.status == "sent"})

    def _near_recently(self, phone: str, now: datetime) -> bool:
        since = now - timedelta(days=self.rules.near_match.max_per_days)
        return any(a.kind == "near" and a.status == "sent"
                   for a in self.repo.list_alerts(self.config.client_id, phone=phone, since=since))

    # ------------------------------------------------------------------ sending

    def _send(self, customer: Customer, picks: list[Pick], lines: list[str], batch: str, now: datetime,
              report: AlertReport) -> None:
        cid, phone, who = self.config.client_id, customer.phone, mask_phone(customer.phone)
        ids = [p.car.id for p in picks]

        def fail(reason: str) -> None:
            for car_id in ids:
                self.repo.mark_alert(cid, phone, car_id, "failed", at=now, reason=reason[:300])
            report.failed += 1
            report.lines.append(f"{who}: FAILED ({reason[:120]})")
            log.error("Alert to %s failed: %s", who, reason)

        template = self.config.whatsapp.templates.get(f"car_alert_{len(picks)}")
        if not template:
            return fail(f"no car_alert_{len(picks)} template configured")
        header = None
        if len(picks) == 1:  # tc_car_alert_1 has a photo header
            photos = self._photos()
            if photos is None:
                return fail("photo header needed but ffmpeg is not available")
            try:
                header = photos.media_id(picks[0].car)
            except (ImageError, WhatsAppError) as exc:
                return fail(f"photo header: {exc}")
        language = self.config.whatsapp.template_languages.get(customer.language, "en")
        try:
            wamid = self.whatsapp.send_template(phone, template, language, lines, header_image_id=header)
        except WhatsAppError as exc:
            return fail(str(exc))

        for car_id in ids:
            self.repo.mark_alert(cid, phone, car_id, "sent", at=now, wa_message_id=wamid)
        # Stored like a reply, so if the customer answers, the agent sees which cars were offered.
        text = "\n".join([f"[Stock alert] {ALERT_INTRO[len(lines)]}", *(f"{i}. {line}" for i, line in
                                                                          enumerate(lines, 1))])
        self.repo.add_message(Message(cid, phone, "out", text, now, source="template", id=wamid, alert_id=batch,
                                      meta={"template": template, "cars": ids}))
        for pick in picks:
            if pick.wish.lead and pick.wish.lead.status == "active":
                pick.wish.lead.status, pick.wish.lead.updated_at = "notified", now
                self.repo.update_lead(pick.wish.lead)
        report.sent += 1
        report.lines.append(f"{who}: SENT {template} with {', '.join(ids)}")
        log.info("Alert %s sent to %s: %s", template, who, ", ".join(ids))
