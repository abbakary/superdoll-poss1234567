from django.core.management.base import BaseCommand
from tracker.models import Salesperson


class Command(BaseCommand):
    help = 'Set up default salespeople'

    def handle(self, *args, **options):
        # Create or update default salespeople
        default_salespeople = [
            {'code': '346', 'name': 'Maria Shayo', 'is_default': False},
            {'code': '401', 'name': 'DCV POS', 'is_default': True},
        ]

        for sp_data in default_salespeople:
            salesperson, created = Salesperson.objects.get_or_create(
                code=sp_data['code'],
                defaults={
                    'name': sp_data['name'],
                    'is_active': True,
                    'is_default': sp_data['is_default'],
                }
            )

            # Update name and status if exists
            if not created:
                salesperson.name = sp_data['name']
                salesperson.is_active = True
                salesperson.is_default = sp_data['is_default']
                salesperson.save()
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Updated salesperson: {salesperson.code} - {salesperson.name}'
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Created salesperson: {salesperson.code} - {salesperson.name}'
                    )
                )

        self.stdout.write(self.style.SUCCESS('✓ Salespeople setup complete'))
