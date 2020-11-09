import * as React from 'react';
import {withTheme, withStyles, Theme} from '@material-ui/core/styles';
import withWidth, {isWidthUp} from '@material-ui/core/withWidth';
import Table from '@material-ui/core/Table';
import TableBody from '@material-ui/core/TableBody';
import TableRow from '@material-ui/core/TableRow';
import TableCell from '@material-ui/core/TableCell';
import ProviderRow from './ProviderRow';
import BaseDialog from '../Dialog/BaseDialog';
import {Breakpoint} from '@material-ui/core/styles/createBreakpoints';
import ProviderPreview from "./ProviderPreview";
import CreateDataPackButton from "./CreateDataPackButton";
import {useEffect} from "react";
import {DepsHashers} from "../../utils/hooks/hooks";
import {shouldDisplay} from "../../utils/generic";
import {RegionJustification} from "./RegionJustification";
import history from "../../utils/history";
import {renderIf} from "../../utils/renderIf";
import {MapZoomLimiter} from "../CreateDataPack/MapZoomLimiter";
import {ProviderRowRegionWrap} from "./ProviderRowRegionWrap";

const jss = (theme: Eventkit.Theme & Theme) => ({
    btn: {
        backgroundColor: theme.eventkit.colors.selected_primary,
        color: theme.eventkit.colors.primary,
        fontWeight: 'bold' as 'bold',
        '&:hover': {
            backgroundColor: theme.eventkit.colors.selected_primary_dark,
            color: theme.eventkit.colors.primary,
        },
        '&:disabled': {
            backgroundColor: theme.eventkit.colors.secondary_dark,
            color: theme.eventkit.colors.grey,
        },
    },
    preview: {
        height: '1000px',
        width: '1000px',
    },
    dialog: {
        margin: '10px',
    }
});

const ZIP_TASK_NAME = 'Project File (.zip)';

interface CalculatorProps {
    fileSizes?: number[],
    setFileSize: (value: number) => void
}

ZipSizeCalculator.defaultProps = {fileSizes: []}

function ZipSizeCalculator(props: CalculatorProps) {
    useEffect(() => {
        props.setFileSize(props.fileSizes.reduce((total, val) => total + val, 0))
    }, [DepsHashers.arrayHash(props.fileSizes)])

    return null;
}


export interface Props {
    providerTasks: Eventkit.ProviderTask[];
    onProviderCancel: (uid: string) => void;
    providers: Eventkit.Provider[];
    job: Eventkit.Job;
    classes: { [className: string]: string };
    theme: Eventkit.Theme & Theme;
    width: Breakpoint;
}

export interface State {
    infoOpen: boolean;
    providerPreviewOpen: boolean;
    selectedProvider?: Eventkit.ProviderTask;
    zipSize?: number;
    hasBlockedRegion: boolean;
}

export class DataPackDetails extends React.Component<Props, State> {
    constructor(props: Props) {
        super(props);
        this.handleInfoOpen = this.handleInfoOpen.bind(this);
        this.handleInfoClose = this.handleInfoClose.bind(this);
        this.selectPreview = this.selectPreview.bind(this);
        this.getPreviewDialogTitle = this.getPreviewDialogTitle.bind(this);
        this.state = {
            infoOpen: false,
            providerPreviewOpen: false,
            hasBlockedRegion: false,
        };
    }

    private getTextFontSize() {
        const {width} = this.props;
        if (!isWidthUp('sm', width)) {
            return '10px';
        } else if (!isWidthUp('md', width)) {
            return '11px';
        } else if (!isWidthUp('lg', width)) {
            return '12px';
        } else if (!isWidthUp('xl', width)) {
            return '13px';
        }
        return '14px';
    }

    private getTableCellWidth() {
        if (!isWidthUp('md', this.props.width)) {
            return '80px';
        }
        return '120px';
    }

    private getToggleCellWidth() {
        return '86px';
    }

    private handleInfoOpen() {
        this.setState({infoOpen: true});
    }

    private handleInfoClose() {
        this.setState({infoOpen: false});
    }

    private selectPreview(providerTask: Eventkit.ProviderTask) {
        this.setState({
            selectedProvider: providerTask,
            providerPreviewOpen: true,
        });
    }

    private getPreviewDialogTitle() {
        const {job} = this.props;
        const {selectedProvider} = this.state;

        let jobElement = (<span>loading...</span>);
        if (!!job) {
            jobElement = (
                <span>{job.name}</span>
            );
        }

        let providerElement = (<span/>);
        if (!!selectedProvider) {
            providerElement = (
                <span> {'>'} {selectedProvider.name}</span>
            );
        }

        return (
            <span>
              Preview: <span style={{fontWeight: 'normal', fontSize: '14px'}}>{jobElement}{providerElement}</span>
          </span>
        );
    }

    render() {
        const {colors} = this.props.theme.eventkit;

        const tableCellWidth = this.getTableCellWidth();
        const toggleCellWidth = this.getToggleCellWidth();
        const textFontSize = this.getTextFontSize();

        const providers = this.props.providerTasks.filter(provider => (provider.display && !provider.hidden));

        const styles = {
            subHeading: {
                fontSize: '16px',
                fontWeight: 'bold' as 'bold',
                color: colors.black,
                alignContent: 'flex-start',
                paddingBottom: '5px',
            },
            download: {
                paddingRight: '12px',
                paddingLeft: '0px',
                fontSize: textFontSize,
                whiteSpace: 'normal' as 'normal',
            },
            genericColumn: {
                paddingRight: '0px',
                paddingLeft: '0px',
                width: tableCellWidth,
                textAlign: 'center' as 'center',
                fontSize: textFontSize,
            },
            info: {
                margin: '5px 10px 5px 5px',
                height: '18px',
                width: '18px',
                cursor: 'pointer',
                fill: colors.primary,
                verticalAlign: 'middle',
            },
        };

        const {classes} = this.props;
        const {selectedProvider} = this.state;

        return (
            <div>
                {renderIf(() => (<>
                    <ZipSizeCalculator
                        fileSizes={
                            this.props.providerTasks.filter(
                                providerTask => shouldDisplay(providerTask)
                            ).map(providerTask => {
                                // Use the zip task if it exists, otherwise calc based on all available.
                                const zipTask = providerTask.tasks.find(task => task.name === ZIP_TASK_NAME);
                                if (!!zipTask && !!zipTask.result.size) {
                                    return Number(zipTask.result.size.replace(' MB', ''));
                                }
                                let fileSize = 0;
                                providerTask.tasks.forEach((task) => {
                                    if (task.result != null) {
                                        if (task.display !== false && task.result.size) {
                                            fileSize = fileSize + Number(task.result.size.replace(' MB', ''));
                                        }
                                    }
                                });
                                return fileSize;
                            })}
                        setFileSize={(value: number) => this.setState({zipSize: value})}
                    />
                    <div className="qa-DataPackDetails-heading" style={styles.subHeading}>
                        Download Options
                    </div>
                    <Table
                        className="qa-DataPackDetails-Table"
                        style={{width: '100%', tableLayout: 'fixed'}}
                    >
                        <TableBody
                            className="qa-DataPackDetails-TableHeader"
                        >
                            <TableRow className="qa-DataPackDetails-TableRow">
                                <TableCell
                                    className="qa-DataPackDetails-TableCell-zipButton"
                                    style={styles.download}
                                >
                                    <CreateDataPackButton
                                        zipSize={this.state.zipSize}
                                        fontSize={textFontSize}
                                        providerTasks={this.props.providerTasks}
                                        job={this.props.job}
                                    />
                                </TableCell>
                                <TableCell
                                    className="qa-DataPackDetails-TableCell-fileSize"
                                    style={styles.genericColumn}
                                >
                                    FILE SIZE
                                </TableCell>
                                <TableCell
                                    className="qa-DataPackDetails-TableCell-estimatedFinish"
                                    style={styles.genericColumn}
                                >
                                    ESTIMATED FINISH
                                </TableCell>
                                <TableCell
                                    className="qa-DataPackDetails-TableCell-progress"
                                    style={styles.genericColumn}
                                >
                                    PROGRESS
                                </TableCell>
                                <TableCell
                                    className="qa-DataPackDetails-TableCell-empty"
                                    style={{...styles.genericColumn, width: toggleCellWidth}}
                                />
                            </TableRow>
                        </TableBody>
                    </Table>
                    <div className="qa-DataPackDetails-providers" id="Providers">
                        <BaseDialog
                            title={this.getPreviewDialogTitle()}
                            bodyStyle={{height: 'auto', width: '100%', maxHeight: 'calc(80vh - 200px)'}}
                            dialogStyle={{margin: '10px', width: '100%'}}
                            innerMaxHeight={1000}
                            show={this.state.providerPreviewOpen}
                            onClose={() => {
                                this.setState({providerPreviewOpen: false})
                            }}
                        >
                            <ProviderPreview
                                providerTasks={this.props.providerTasks}
                                selectedProvider={(!!selectedProvider) ? selectedProvider.slug : ''}
                                selectProvider={this.selectPreview}
                            />
                        </BaseDialog>
                        {providers.map((provider, ix) => (
                            <ProviderRowRegionWrap
                                extents={(() => {
                                    const extentArray = [];
                                    if (this.props?.job?.extent) {
                                        extentArray.push(this.props?.job?.extent);
                                    }
                                    return extentArray;
                                })()}
                                backgroundColor={ix % 2 === 0 ? colors.secondary : colors.white}
                                key={provider.uid}
                                onProviderCancel={this.props.onProviderCancel}
                                providerTask={provider}
                                job={this.props.job}
                                selectProvider={this.selectPreview}
                                providers={this.props.providers}
                            />
                        ))}
                    </div>
                </>), !this.state.hasBlockedRegion)}
            </div>
        );
    }
}

export default withWidth()(withTheme(withStyles(jss)(DataPackDetails)));
