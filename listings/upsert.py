from django.utils import timezone

from listings.models import Agent, Listing, PriceHistory


def upsert(parsed):
    agent = None
    if parsed.agent_source_id:
        agent, _ = Agent.objects.update_or_create(
            source_site=parsed.source_site,
            source_id=parsed.agent_source_id,
            defaults={"name": parsed.agent_name},
        )

    existing = Listing.objects.filter(
        source_site=parsed.source_site, source_id=parsed.source_id
    ).first()
    # PriceHistory.price is NOT NULL (§5.3), so a "Thỏa thuận" (null-price)
    # observation can't be recorded as a history row; the Listing itself
    # still gets its price overwritten to null below.
    if existing and parsed.price != existing.price and parsed.price is not None:
        PriceHistory.objects.create(
            listing=existing,
            price=parsed.price,
            price_per_sqm=parsed.price_per_sqm,
            observed_at=timezone.now(),
        )
    listing, created = Listing.objects.update_or_create(
        source_site=parsed.source_site,
        source_id=parsed.source_id,
        defaults={
            **parsed.fields,
            "agent": agent,
            "last_seen_at": timezone.now(),
            "is_active": True,
        },
    )
    if created and listing.price is not None:
        PriceHistory.objects.create(
            listing=listing,
            price=listing.price,
            price_per_sqm=listing.price_per_sqm,
            observed_at=timezone.now(),
        )
    return created
