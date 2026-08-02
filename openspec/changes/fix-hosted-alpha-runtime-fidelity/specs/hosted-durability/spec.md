## ADDED Requirements

### Requirement: Vault backup cadence is daily with a stated 24-hour objective
The system durability worker SHALL create one complete encrypted vault archive per day
rather than one every 30 minutes, and the declared vault recovery point objective SHALL
be 24 hours. Retention SHALL remain 30 days with seven-day Object Lock. The cadence
SHALL NOT be raised without either content-addressed incremental storage or an accepted
cost and availability analysis, because each run quiesces the cell and each retained
archive is a full independent copy.

The stated objective SHALL match what tenants are told. A tighter objective SHALL NOT be
advertised while the implementation stores full archives.

#### Scenario: Volume is lost between daily archives
- **WHEN** a cell's volume is lost and the most recent archive is up to 24 hours old
- **THEN** restore proceeds from that archive and the loss window is within the declared objective

#### Scenario: Someone proposes a shorter interval
- **WHEN** a change would increase archive frequency while archives remain full copies
- **THEN** it is rejected until incremental storage exists, because storage multiplies by run count and each run costs cell availability

#### Scenario: Retention depth is what recovers from error
- **WHEN** a tenant or operator discovers days later that data was damaged by a bad write or migration
- **THEN** recovery uses the 30-day retention depth rather than cadence, which is the failure mode that actually occurs
