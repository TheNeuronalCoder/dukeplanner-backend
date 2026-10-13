from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('student', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='semester',
            name='credits_attempted',
        ),
        migrations.RemoveField(
            model_name='semester',
            name='credits_earned',
        ),
        migrations.AddField(
            model_name='class',
            name='credits',
            field=models.PositiveSmallIntegerField(default=3),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='class',
            name='credits_earned',
            field=models.BooleanField(default=True),
        ),
    ]
