import * as React from 'react';
import Info from '@material-ui/icons/Info';
import BaseDialog from "../Dialog/BaseDialog";

interface Props {
    title?: string;
    iconProps?: any;
}

interface State {
    displayDialog: boolean;
}

export class InfoDialog extends React.Component<Props, State> {

    static defaultProps;

    constructor(props: Props) {
        super(props);
        this.state = {
            displayDialog: false,
        };
        this.openDialog = this.openDialog.bind(this);
    }

    openDialog() {
        this.setState({displayDialog: true})
    }

    render() {
        const { title, iconProps } = this.props;

        return (
            <div className="InfoDialog">
                <Info
                    {...((!!iconProps) ? iconProps : {})}
                    className={`qa-Estimate-Info-Icon`}
                    onClick={this.openDialog}
                    color="primary"
                />
                <BaseDialog
                    show={this.state.displayDialog}
                    title={(!!title) ? title : undefined}
                    onClose={() =>  this.setState({displayDialog: false})}
                >
                    <div
                        style={{paddingBottom: '10px', wordWrap: 'break-word'}}
                    >
                        {this.props.children}
                    </div>
                </BaseDialog>
            </div>
        );
    }
}

export default InfoDialog;