"""Say "cooperative" everywhere the public site says "society".

The landing page's copy lives in these tables, not in the React bundle, so
renaming the wording in code alone would leave a deployed site still reading
"society" — the seeded rows are what actually render. This rewrites them.

Word-level rather than whole-value matching, deliberately. Comparing against
the old seeded paragraphs would skip any row an admin had since edited, which
is exactly the copy most likely to be read. Replacing the word in place changes
the one thing that is wrong and leaves every other edit intact — and running it
twice is a no-op.

Only the marketing-copy tables are touched. A cooperative's own registered name
often legitimately contains "Society" (it is the standard legal form in several
countries), and those live on `tenants.Cooperative`, which this never opens.
"""
from django.db import migrations

# Plural first: replacing "society" first would leave "societiesy".
REPLACEMENTS = (
    ("Societies", "Cooperatives"),
    ("societies", "cooperatives"),
    ("Society", "Cooperative"),
    ("society", "cooperative"),
)

# Possessives need no special case — the apostrophe follows the stem, so
# "society's" and "society’s" both fall out of replacing the word itself.

TEXT_FIELDS = {
    "SiteContent": (
        "hero_eyebrow", "hero_title", "hero_subtitle", "hero_primary_cta",
        "hero_secondary_cta", "hero_image_alt",
        "principle_eyebrow", "principle_title", "principle_body",
        "features_title", "features_intro",
        "showcase_title", "showcase_intro",
        "steps_title", "steps_intro",
        "gallery_title", "gallery_intro",
        "apps_title", "apps_body", "apps_note", "app_screenshot_alt",
        "pricing_title", "pricing_intro", "pricing_fallback",
        "cta_title", "cta_body", "cta_button", "cta_image_alt",
        "footer_tagline", "footer_disclaimer",
        "meta_title", "meta_description",
    ),
    "SiteFeature": ("title", "body"),
    "SiteStep": ("title", "body"),
    "SiteShowcase": ("title", "body", "bullets", "image_alt"),
    "SiteTrustBadge": ("text",),
    "SiteGalleryImage": ("alt_text", "caption"),
}


def _rename(value: str) -> str:
    for old, new in REPLACEMENTS:
        value = value.replace(old, new)
    return value


def rename_forwards(apps_registry, schema_editor):
    for model_name, fields in TEXT_FIELDS.items():
        model = apps_registry.get_model("platform_admin", model_name)
        for row in model.objects.all():
            changed = []
            for field in fields:
                current = getattr(row, field) or ""
                renamed = _rename(current)
                if renamed != current:
                    setattr(row, field, renamed)
                    changed.append(field)
            if changed:
                row.save(update_fields=changed)


def rename_backwards(apps_registry, schema_editor):
    """Not reversed.

    Going back would have to rewrite "cooperative" as "society" everywhere,
    including the many places that always said "cooperative" and should not
    change. Leaving the copy as it is on a reverse is the lesser harm; the
    wording is editable in the admin either way.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("platform_admin", "0010_alter_sitecontent_cta_body_and_more"),
    ]

    operations = [
        migrations.RunPython(rename_forwards, rename_backwards),
    ]
