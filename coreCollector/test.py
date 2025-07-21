from typing import List


def binary_search(nums: [int], val):
    upper = len(nums) - 1
    lower = 0

    while lower < upper:
        mid = (upper + lower) // 2
        if nums[mid] <= val:
            lower = mid + 1
        else:
            upper = mid
    return len(nums) - lower


print(binary_search([1,2,3,4,6,7,8,9,10,11], 5))
