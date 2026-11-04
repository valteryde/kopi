from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kopi", "0004_operate"),
    ]

    operations = [
        migrations.AddField(
            model_name="link",
            name="enabled",
            field=models.BooleanField(default=True),
        ),
    ]
