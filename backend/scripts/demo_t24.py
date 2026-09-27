"""Наглядный прогон денежного пути саги CreatePayment (T-2.4).

Не тест — демонстрация для ревью: показывает, что происходит с деньгами и
статусами в каждом из четырёх исходов. Запуск: ``uv run python scripts/demo_t24.py``
из каталога ``backend``.
"""

import asyncio
from decimal import Decimal

from tests.unit.application.fakes import SagaEnvironment

from src.core_service.application.dto import CreatePaymentInput
from src.core_service.application.ports.payment_provider import ProviderStatus
from src.core_service.domain.entities.account import Account
from src.core_service.domain.exceptions import PaymentProviderError
from src.core_service.domain.value_objects.currency import Currency
from src.core_service.domain.value_objects.identifiers import AccountId
from src.core_service.domain.value_objects.money import Money

BALANCE = Decimal('1000.00')
AMOUNT = Decimal('250.00')


def _input(account_id: AccountId) -> CreatePaymentInput:
    return CreatePaymentInput(
        from_account_id=account_id,
        amount=Money.from_number(AMOUNT, Currency.RUB),
        idempotency_key='k1',
    )


def _banner(title: str) -> None:
    print()
    print('=' * 72)
    print(title)
    print('=' * 72)


async def main() -> None:
    _banner('1. УСПЕХ: провайдер принял платёж (PROCESSING)')
    env = SagaEnvironment.build(status=ProviderStatus.PROCESSING)
    account = env.add_account(Account(AccountId.new(), Money.from_number(BALANCE, Currency.RUB)))
    data = _input(account.id)
    result = await env.build_use_case().execute(data)
    print(f'  баланс: {BALANCE} -> {env.account_balance(account.id)}  (удерживаем в холде)')
    print(f'  платёж: {result.status}, provider_id={result.provider_payment_id}')
    print(f'  провайдер вызван вне транзакции БД: {env.provider.open_uow_during_calls == [False]}')
    print(f'  коммитов: {env.uow_factory.commits}')

    _banner('2. ПОВТОР с тем же ключом: ни второго платежа, ни второго списания')
    balance_before = env.account_balance(account.id)
    repeated = await env.build_use_case().execute(data)
    print(f'  тот же payment_id: {result.payment_id == repeated.payment_id}')
    print(f'  платежей в базе: {len(env.database.payments)}')
    print(f'  баланс не изменился: {balance_before == env.account_balance(account.id)}')
    print(f'  вызовов провайдера: {env.provider.call_count} (был 1)')

    _banner('3. БИЗНЕС-ОТКАЗ (FAILED): исход ИЗВЕСТЕН -> деньги ВОЗВРАЩАЮТСЯ')
    rejected = SagaEnvironment.build(status=ProviderStatus.FAILED)
    payer = rejected.add_account(Account(AccountId.new(), Money.from_number(BALANCE, Currency.RUB)))
    rejection = await rejected.build_use_case().execute(_input(payer.id))
    print(f'  баланс после отказа: {rejected.account_balance(payer.id)}  <- деньги вернулись')
    print(f'  платёж: {rejection.status}')
    print(f'  события: {rejected.event_publisher.types_published()}')

    _banner('4. ТЕХНИЧЕСКИЙ СБОЙ: исход НЕИЗВЕСТЕН -> холд ОСТАЁТСЯ')
    broken = SagaEnvironment.build(error=PaymentProviderError('провайдер недоступен'))
    holder = broken.add_account(Account(AccountId.new(), Money.from_number(BALANCE, Currency.RUB)))
    try:
        await broken.build_use_case().execute(_input(holder.id))
    except PaymentProviderError as error:
        print(f'  ошибка ушла наружу: {error}')
    print(f'  баланс: {broken.account_balance(holder.id)}  <- холд остался, возврат = риск двойного')
    print(f'  платёж: {broken.only_payment().status} (ждёт сверку T-2.10)')
    print(f'  события: {broken.event_publisher.types_published()}')


if __name__ == '__main__':
    asyncio.run(main())
