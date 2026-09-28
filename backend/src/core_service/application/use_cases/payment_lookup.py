"""Чтение платежа сценарием: единственное место этого правила.

Три сценария — чтение (T-2.5), отмена (T-2.6) и будущий вебхук (T-2.7) — начинаются
одинаково: открыть короткую транзакцию, найти платёж, а если его нет **упасть**, а не
отдать ``None``. У каждого была бы своя копия, и тогда «платёж не найден» в одном
сценарии означало бы доменную ошибку, а в другом — ``None``, который транспорт
(ЭПИК 6/7) начнёт трактовать по-своему: один путь отдаст 404, другой — 500.

Транзакция здесь намеренно короткая и закрывается сразу: читать платёж, чтобы
узнать, какой счёт блокировать, не стоит под блокировкой строк, а читать его перед
сетевым вызовом (T-2.5) — тем более. Решение принимается по свежему прочтению уже
под нужными блокировками.
"""

from collections.abc import Callable

from src.core_service.application.ports.unit_of_work import UnitOfWork
from src.core_service.domain.entities.payment import Payment
from src.core_service.domain.exceptions import EntityNotFoundError
from src.core_service.domain.value_objects.identifiers import PaymentId


async def read_payment(uow_factory: Callable[[], UnitOfWork], payment_id: PaymentId) -> Payment:
    """Читает платёж в отдельной транзакции, которая сразу закрывается.

    :param uow_factory: фабрика единицы работы; транзакция открывается и
        закрывается внутри, наружу отдаётся только сущность;
    :param payment_id: идентификатор искомого платежа;
    :returns: платёж из хранилища;
    :raises EntityNotFoundError: платежа с таким идентификатором нет.
    """
    async with uow_factory() as uow:
        payment = await uow.payments.get(payment_id)
        if payment is None:
            raise EntityNotFoundError(f'Платёж {payment_id} не найден')
        return payment
