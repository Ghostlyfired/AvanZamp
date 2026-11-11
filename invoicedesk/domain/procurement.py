"""Shared exact billing display and monotonic identifier helpers."""
from decimal import Decimal
import re


def next_vendor_number(ids, previous=0):
    return max([previous, *(int(m.group(1)) for value in ids
                 if (m := re.fullmatch(r'V-?(\d+)', value, re.IGNORECASE)))]) + 1


def fulfillment(net, approved):
    net, approved = Decimal(net), Decimal(approved)
    return dict(approved_amount=str(approved), remaining_amount=str(net-approved),
        overage_amount=str(max(Decimal(0), approved-net)), fully_approved=approved >= net,
        fulfillment_percent=str((approved/net*100).quantize(Decimal('0.01'))) if net else '0',
        fulfillment_status=('Overbilled' if approved > net else 'Fulfilled' if approved == net
                            else 'Partially fulfilled' if approved else 'Unfulfilled'))
