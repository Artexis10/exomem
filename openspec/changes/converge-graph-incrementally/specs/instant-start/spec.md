## ADDED Requirements

### Requirement: Snapshot adoption carries created and removed pages as residue

A process proving a graph snapshot it did not publish, through a caller that can carry
a residue, SHALL treat a page admitted on disk but absent from the snapshot's file rows
(created) and a page present in the snapshot's file rows but absent from disk (removed)
as residue, exactly as it treats a page whose bytes differ. The combined residue SHALL
stay bounded by one drain pass. A snapshot row for a page that still exists on disk but
is no longer admitted SHALL still decline as a membership difference. A resolver
topology difference SHALL be accepted only when reverting every residue path's resolver
entry to what the snapshot recorded reproduces the snapshot's stored topology
fingerprint; any other topology difference SHALL still decline. An unreadable or
unparseable source, a recall policy that moved, and a projection that moved during the
proof SHALL still decline. The adopted residue SHALL be queued as incremental repair
with the availability marker withdrawn, and the repair SHALL add the rows of a created
page and delete the rows of a removed one.

#### Scenario: A page created after the last publication is adopted as residue

- **WHEN** a page is committed after the serving worker's last graph publication and a
  replacement proves that snapshot
- **THEN** the snapshot is adopted with the created page in its residue, reads that
  require a current projection refuse until the repair lands, and one drain adds the
  page's rows and republishes the marker with no whole-vault rebuild

#### Scenario: A page removed after the last publication is adopted as residue

- **WHEN** a page the snapshot indexes is deleted before a replacement proves it
- **THEN** the snapshot is adopted with the removed page in its residue, and one drain
  deletes the page's rows and republishes the marker with no whole-vault rebuild

#### Scenario: An unexplained topology change still declines

- **WHEN** the resolver topology differs from the snapshot's in a way that reverting the
  residue paths does not reproduce
- **THEN** adoption declines with the topology reason and the caller pays the
  whole-vault pass as before
