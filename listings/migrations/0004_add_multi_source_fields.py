from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("listings", "0003_widen_url_max_length"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agent",
            name="source_site",
            field=models.CharField(
                choices=[
                    ("alonhadat", "alonhadat"),
                    ("homedy", "homedy"),
                    ("batdongsan", "batdongsan"),
                    ("maisonoffice", "maisonoffice"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="listing",
            name="source_site",
            field=models.CharField(
                choices=[
                    ("alonhadat", "alonhadat"),
                    ("homedy", "homedy"),
                    ("batdongsan", "batdongsan"),
                    ("maisonoffice", "maisonoffice"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="listing",
            name="category_id_source",
            field=models.IntegerField(null=True),
        ),
        migrations.AddField(
            model_name="scraperun",
            name="source_site",
            field=models.CharField(
                choices=[
                    ("alonhadat", "alonhadat"),
                    ("homedy", "homedy"),
                    ("batdongsan", "batdongsan"),
                ],
                default="batdongsan",
                max_length=20,
            ),
            preserve_default=False,
        ),
    ]
