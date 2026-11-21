# Knowledge workflow acceptance fixture

These notes describe a fictional release exercise. The important failure mode is a
duplicate merge notification after a review runner loses its acknowledgement.
A closed review without a GitHub merge commit must never publish a knowledge entry.

On the merged event, check the repository identity, pull request number, and reviewed head.
Persist the actual merge commit beside the published knowledge version. If the same
event is delivered again, look up the existing settlement and return the original entry.
Confirm the result through the project knowledge API before treating the run as complete.
